import json
from typing import Optional

import pandas as pd

from uwazi_api.adapters.http_client_adapter import HttpClientAdapter
from uwazi_api.domain.entity import Entity
from uwazi_api.domain.exceptions import SearchError
from uwazi_api.domain.sanitize_property_label import PropertyLabelSanitizer
from uwazi_api.domain.search_filters import SearchFilters, SelectFilter
from uwazi_api.domain.search_pages import search_page_sizes
from uwazi_api.domain.thesauri_label import label_id_map, resolve_labels_to_ids
from uwazi_api.use_cases.entity_to_dataframe import entities_to_dataframe
from uwazi_api.use_cases.repositories.template_repository import TemplateRepository, existing_template_scope
from uwazi_api.use_cases.repositories.thesauri_repository import ThesauriRepository


class SearchRepository:
    def __init__(
        self,
        http_client: HttpClientAdapter,
        template_repo: Optional[TemplateRepository] = None,
        thesauri_repo: Optional[ThesauriRepository] = None,
    ):
        self.http = http_client
        self._template_repo = template_repo
        self._thesauri_repo = thesauri_repo

    def get_shared_ids(self, to_process_template: str, batch_size: int, unpublished: bool = True) -> list[str]:
        template_id = self._resolve_template_id(to_process_template)
        params = {
            "_types": f'["{template_id}"]',
            "types": f'["{template_id}"]',
            "unpublished": "true" if unpublished else "false",
            "limit": batch_size,
            "order": "desc",
            "sort": "creationDate",
        }
        response = self.http.request_adapter.get(
            f"{self.http.url}/api/search",
            headers=self.http.headers,
            params=params,
            cookies={"locale": "en"},
        )
        if response.status_code != 200:
            raise SearchError(
                f"Error getting entities shared ids: status={response.status_code}, body={response.text[:500]}"
            )
        rows = json.loads(response.content).get("rows", [])
        return [row["sharedId"] for row in rows]

    def get(
        self,
        start_from: int = 0,
        batch_size: int = 30,
        template_name: Optional[str] = None,
        language: str = "en",
        published: Optional[bool] = None,
    ) -> list[Entity]:
        """Fetch entities, transparently paging under Elasticsearch's
        ``index.max_result_window`` (10 000) via ``from``/``limit`` windows.

        Every request stays inside the window (``from + size <= 10 000``), so a
        ``batch_size`` larger than it is served as the reachable prefix rather
        than failing the whole call. ``start_from`` offsets the first returned
        entity within that paged stream.
        """
        collected: list[Entity] = []
        for page_from, page_size in search_page_sizes(start_from, batch_size):
            params = {
                "from": page_from,
                "limit": page_size,
                "allAggregations": "false",
                "sort": "creationDate",
                "order": "desc",
            }
            if template_name:
                template_id = self._resolve_template_id(template_name)
                params["types"] = f'["{template_id}"]'
            else:
                types_scope = existing_template_scope(self._template_repo)
                if types_scope:
                    params["types"] = types_scope
            params["includeUnpublished"] = "false" if published else "true"

            response = self.http.request_adapter.get(
                f"{self.http.url}/api/search",
                headers=self.http.headers,
                params=params,
                cookies={"locale": language},
            )
            if response.status_code != 200:
                raise SearchError(f"Error getting entities: status={response.status_code}, body={response.text[:500]}")
            rows = json.loads(response.content).get("rows", [])
            collected.extend(Entity.model_validate(row) for row in rows)
            if len(rows) < page_size:
                break  # exhausted the matching set
        return collected[:batch_size]

    def search_by_text(
        self,
        search_term: str,
        template_name: Optional[str] = None,
        start_from: int = 0,
        batch_size: int = 30,
        language: str = "en",
    ) -> list[Entity]:
        params = {
            "allAggregations": "false",
            "from": start_from,
            "includeUnpublished": "true",
            "limit": batch_size,
            "order": "desc",
            "searchTerm": search_term,
            "sort": "_score",
            "treatAs": "number",
            "unpublished": "false",
            "aggregateGeneratedToc": "true",
            "aggregatePublishingStatus": "true",
            "aggregatePermissionsByUsers": "true",
        }
        if template_name:
            template_id = self._resolve_template_id(template_name)
            params["types"] = f'["{template_id}"]'

        return self._execute_search(params, language)

    def search_by_filter(
        self,
        filters: SearchFilters,
        template_name: Optional[str] = None,
        start_from: int = 0,
        batch_size: int = 30,
        language: str = "en",
        published: Optional[bool] = None,
        order: str = "desc",
        sort: str = "creationDate",
    ) -> list[Entity]:
        template_id = self._resolve_template_id(template_name) if template_name else None
        resolved = self._resolve_filters(filters, template_id, language)
        serialized_filters = self._serialize_filters(resolved)
        params = self._build_filter_search_params(
            serialized_filters, template_id, start_from, batch_size, published, order, sort
        )
        return self._execute_search(params, language)

    def _resolve_filters(self, filters: SearchFilters, template_id: Optional[str], language: str) -> dict:
        """``{sanitized property name: filter}`` with thesaurus labels resolved to ids.

        Pure: the caller's ``SearchFilters`` is never modified. Callers that
        page through a result set reuse one filter object across requests, and
        an in-place label→id rewrite made the second request re-resolve an id as
        if it were a label (``Value '<uuid>' not found in thesaurus ...``).
        """
        if not template_id or not self._template_repo:
            return dict(filters.filters)
        resolved: dict = {}
        for prop_name, filter_value in filters.filters.items():
            prop = self._template_repo.find_property(template_id, prop_name)
            if not prop:
                raise SearchError(f"Property '{prop_name}' not found in template {template_id}")
            self._template_repo.ensure_property_filterable(prop, prop_name)
            if isinstance(filter_value, SelectFilter) and prop.type in ("select", "multiselect"):
                filter_value = self._resolve_select_filter(filter_value, prop, prop_name, language)
            resolved[PropertyLabelSanitizer.sanitize(prop.name)] = filter_value
        return resolved

    def _resolve_template_id(self, template_name_or_id: str) -> str:
        if not self._template_repo:
            return template_name_or_id
        template_id = self._template_repo.resolve_template_id(template_name_or_id)
        if not template_id:
            raise SearchError(f"Template '{template_name_or_id}' not found")
        return template_id

    def _resolve_select_filter(self, filter_value: SelectFilter, prop, prop_name: str, language: str) -> SelectFilter:
        """Resolve a select filter's labels to thesaurus ids in a NEW filter.

        Values that are already ids (e.g. a caller resolved them once and
        passes them again) are kept as-is, so resolution is idempotent.
        """
        if not filter_value.values:
            return filter_value
        thesauri_id = prop.content
        if not thesauri_id:
            raise SearchError(f"Property '{prop_name}' has no thesauri content")
        thesauri = self._find_thesaurus(thesauri_id, prop_name, language)
        return SelectFilter(
            values=resolve_labels_to_ids(
                list(filter_value.values), label_id_map(thesauri.values), thesauri.name, prop_name
            )
        )

    def _find_thesaurus(self, thesauri_id: str, prop_name: str, language: str):
        thesauri_list = self._thesauri_repo.get(language=language)
        thesauri = next((t for t in thesauri_list if t.id == thesauri_id), None)
        if not thesauri:
            raise SearchError(f"Thesauri for property '{prop_name}' not found")
        return thesauri

    def _serialize_filters(self, resolved_filters: dict) -> dict:
        result = {}
        for key, value in resolved_filters.items():
            result[key] = value.model_dump(exclude_none=True) if hasattr(value, "model_dump") else value
        return result

    def _build_filter_search_params(
        self,
        serialized_filters: dict,
        template_id: Optional[str],
        start_from: int,
        batch_size: int,
        published: Optional[bool],
        order: str,
        sort: str,
    ) -> dict:
        params = {
            "allAggregations": "false",
            "filters": json.dumps(serialized_filters),
            "from": start_from,
            "includeUnpublished": "false" if published else "true",
            "limit": batch_size,
            "order": order,
            "sort": sort,
            "unpublished": "false",
            "aggregatePublishingStatus": "true",
            "aggregatePermissionsByUsers": "true",
            "include": '["permissions"]',
        }
        if template_id:
            params["types"] = f'["{template_id}"]'
        return params

    def _execute_search(self, params: dict, language: str) -> list[Entity]:
        if "types" not in params:
            types_scope = existing_template_scope(self._template_repo)
            if types_scope:
                params["types"] = types_scope
        response = self.http.request_adapter.get(
            f"{self.http.url}/api/search",
            headers=self.http.headers,
            params=params,
            cookies={"locale": language},
        )
        if response.status_code != 200:
            raise SearchError(
                f"Error searching entities by filter: status={response.status_code}, body={response.text[:500]}"
            )
        rows = json.loads(response.content).get("rows", [])
        return [Entity.model_validate(row) for row in rows]

    def search_by_filter_to_dataframe(
        self,
        filters: SearchFilters,
        template_name: Optional[str] = None,
        start_from: int = 0,
        batch_size: int = 30,
        language: str = "en",
        published: Optional[bool] = None,
        order: str = "desc",
        sort: str = "creationDate",
    ) -> pd.DataFrame:
        template_id = self._resolve_template_id(template_name) if template_name else None
        resolved = self._resolve_filters(filters, template_id, language)
        serialized_filters = self._serialize_filters(resolved)
        params = self._build_filter_search_params(
            serialized_filters, template_id, start_from, batch_size, published, order, sort
        )
        entities = self._execute_search(params, language)
        return entities_to_dataframe(entities, template_name, self._template_repo)

    def search_to_dataframe(
        self,
        template_name: Optional[str] = None,
        start_from: int = 0,
        batch_size: int = 30,
        language: str = "en",
        published: Optional[bool] = None,
        order: str = "desc",
        sort: str = "creationDate",
    ) -> pd.DataFrame:
        return self.search_by_filter_to_dataframe(
            SearchFilters(), template_name, start_from, batch_size, language, published, order, sort
        )
