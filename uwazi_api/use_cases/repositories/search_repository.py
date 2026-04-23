import json
from typing import List, Optional

from uwazi_api.domain.entity import Entity
from uwazi_api.domain.exceptions import SearchError
from uwazi_api.domain.search_filters import SearchFilters, DateRange, SelectFilter
from uwazi_api.use_cases.repositories.template_repository import TemplateRepository
from uwazi_api.use_cases.repositories.thesauri_repository import ThesauriRepository
from uwazi_api.adapters.http_client_adapter import HttpClientAdapter


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

    def get_shared_ids(self, to_process_template: str, batch_size: int, unpublished: bool = True) -> List[str]:
        params = {
            "_types": f'["{to_process_template}"]',
            "types": f'["{to_process_template}"]',
            "unpublished": "true" if unpublished else "false",
            "limit": batch_size,
            "order": "desc",
            "sort": "creationDate",
        }
        response = self.http.request_adapter.get(
            f"{self.http.url}/api/search",
            headers=self.http.headers,
            params=params,
            cookies={"connect.sid": self.http.connect_sid, "locale": "en"},
        )
        if response.status_code != 200:
            raise SearchError("Error getting entities shared ids")
        rows = json.loads(response.text).get("rows", [])
        return [row["sharedId"] for row in rows]

    def get(
        self,
        start_from: int = 0,
        batch_size: int = 30,
        template_id: Optional[str] = None,
        language: str = "en",
        published: Optional[bool] = None,
    ) -> List[Entity]:
        params = {
            "from": start_from,
            "limit": batch_size,
            "allAggregations": "false",
            "sort": "creationDate",
            "order": "desc",
        }
        if template_id:
            params["types"] = f'["{template_id}"]'
        params["includeUnpublished"] = "false" if published else "true"

        response = self.http.request_adapter.get(
            f"{self.http.url}/api/search",
            headers=self.http.headers,
            params=params,
            cookies={"connect.sid": self.http.connect_sid, "locale": language},
        )
        if response.status_code != 200:
            raise SearchError("Error getting entities")
        rows = json.loads(response.text).get("rows", [])
        return [Entity.model_validate(row) for row in rows]

    def search_by_text(
        self,
        search_term: str,
        template_id: Optional[str] = None,
        start_from: int = 0,
        batch_size: int = 30,
        language: str = "en",
    ) -> List[Entity]:
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
        if template_id:
            params["types"] = f'["{template_id}"]'

        return self._execute_search(params, language)

    def search_by_filter(
        self,
        filters: SearchFilters,
        template_id: Optional[str] = None,
        start_from: int = 0,
        batch_size: int = 30,
        language: str = "en",
        published: Optional[bool] = None,
        order: str = "desc",
        sort: str = "creationDate",
    ) -> List[Entity]:
        self._validate_and_resolve_filters(filters, template_id, language)
        serialized_filters = self._serialize_filters(filters)
        params = self._build_filter_search_params(
            serialized_filters, template_id, start_from, batch_size, published, order, sort
        )
        return self._execute_search(params, language)

    def _validate_and_resolve_filters(self, filters: SearchFilters, template_id: Optional[str], language: str) -> None:
        if not template_id or not self._template_repo:
            return
        template = self._template_repo.get_by_id(template_id)
        if not template:
            return
        all_props = template.properties + template.common_properties
        for prop_name, filter_value in filters.filters.items():
            prop = self._find_template_property(all_props, prop_name, template_id)
            self._ensure_property_filterable(prop, prop_name)
            if isinstance(filter_value, SelectFilter) and prop.type in ("select", "multiselect"):
                self._resolve_select_filter(filter_value, prop, prop_name, language)

    def _find_template_property(self, all_props, prop_name: str, template_id: str):
        prop = next((p for p in all_props if p.name == prop_name), None)
        if prop is None:
            normalized = self._normalize_name(prop_name)
            prop = next((p for p in all_props if self._normalize_name(p.name) == normalized), None)
        if not prop:
            raise SearchError(f"Property '{prop_name}' not found in template {template_id}")
        return prop

    @staticmethod
    def _normalize_name(name: str) -> str:
        return "".join(ch if ch.isalnum() else "_" for ch in name.lower())

    def _ensure_property_filterable(self, prop, prop_name: str) -> None:
        if not prop.filter:
            raise SearchError(f"Property '{prop_name}' is not filterable")

    def _resolve_select_filter(self, filter_value: SelectFilter, prop, prop_name: str, language: str) -> None:
        if not filter_value.values:
            return
        thesauri_id = prop.content
        if not thesauri_id:
            raise SearchError(f"Property '{prop_name}' has no thesauri content")
        thesauri = self._find_thesaurus(thesauri_id, prop_name, language)
        name_to_id = {v.label: v.id for v in thesauri.values}
        filter_value.values = [
            name_to_id[name]
            for name in filter_value.values
            if self._validate_thesaurus_value(name, name_to_id, thesauri, prop_name)
        ]

    def _find_thesaurus(self, thesauri_id: str, prop_name: str, language: str):
        thesauri_list = self._thesauri_repo.get(language=language)
        thesauri = next((t for t in thesauri_list if t.id == thesauri_id), None)
        if not thesauri:
            raise SearchError(f"Thesauri for property '{prop_name}' not found")
        return thesauri

    def _validate_thesaurus_value(self, name: str, name_to_id: dict, thesauri, prop_name: str) -> bool:
        if name not in name_to_id:
            raise SearchError(f"Value '{name}' not found in thesaurus '{thesauri.name}' for property '{prop_name}'")
        return True

    def _serialize_filters(self, filters: SearchFilters) -> dict:
        return {
            name: (value.model_dump(exclude_none=True) if hasattr(value, "model_dump") else value)
            for name, value in filters.filters.items()
        }

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

    def _execute_search(self, params: dict, language: str) -> List[Entity]:
        response = self.http.request_adapter.get(
            f"{self.http.url}/api/search",
            headers=self.http.headers,
            params=params,
            cookies={"connect.sid": self.http.connect_sid, "locale": language},
        )
        if response.status_code != 200:
            raise SearchError(f"Error searching entities by filter: {response.status_code}")
        rows = json.loads(response.text).get("rows", [])
        return [Entity.model_validate(row) for row in rows]
