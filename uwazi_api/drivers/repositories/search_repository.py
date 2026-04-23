import json
from typing import List, Optional

from uwazi_api.domain.entity import Entity
from uwazi_api.domain.exceptions import SearchError
from uwazi_api.drivers.http_client import HttpClient


class SearchRepository:
    def __init__(self, http_client: HttpClient):
        self.http = http_client

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
