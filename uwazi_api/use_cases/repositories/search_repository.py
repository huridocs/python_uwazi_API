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

        response = self.http.request_adapter.get(
            f"{self.http.url}/api/search",
            headers=self.http.headers,
            params=params,
            cookies={"connect.sid": self.http.connect_sid, "locale": language},
        )
        if response.status_code != 200:
            raise SearchError(f"Error searching entities by text: {response.status_code}")
        rows = json.loads(response.text).get("rows", [])
        return [Entity.model_validate(row) for row in rows]

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
        if template_id and self._template_repo:
            template = self._template_repo.get_by_id(template_id)
            if template:
                all_props = template.properties + template.common_properties
                for prop_name, filter_value in filters.filters.items():
                    prop = next((p for p in all_props if p.name == prop_name), None)
                    if not prop:
                        raise SearchError(f"Property '{prop_name}' not found in template {template_id}")
                    if not prop.filter:
                        raise SearchError(f"Property '{prop_name}' is not filterable")
                    if isinstance(filter_value, SelectFilter) and prop.type in ("select", "multiselect"):
                        if not filter_value.values:
                            continue
                        thesauri_id = prop.content
                        if not thesauri_id:
                            raise SearchError(f"Property '{prop_name}' has no thesauri content")
                        thesauri_list = self._thesauri_repo.get(language=language)
                        thesauri = next((t for t in thesauri_list if t.id == thesauri_id), None)
                        if not thesauri:
                            raise SearchError(f"Thesauri for property '{prop_name}' not found")
                        name_to_id = {v.label: v.id for v in thesauri.values}
                        resolved = []
                        for name in filter_value.values:
                            if name in name_to_id:
                                resolved.append(name_to_id[name])
                            else:
                                raise SearchError(
                                    f"Value '{name}' not found in thesaurus '{thesauri.name}' for property '{prop_name}'"
                                )
                        filter_value.values = resolved

        serialized_filters = {
            name: (value.model_dump(exclude_none=True) if hasattr(value, "model_dump") else value)
            for name, value in filters.filters.items()
        }

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
