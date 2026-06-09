import json

from uwazi_api.adapters.http_client_adapter import HttpClientAdapter
from uwazi_api.domain.exceptions import PageNotFoundError
from uwazi_api.domain.page import Page


class PagesRepository:
    def __init__(self, http_client: HttpClientAdapter):
        self.http = http_client

    def get_all(self) -> list[Page]:
        response = self.http.request_adapter.get(
            url=f"{self.http.url}/api/pages",
            headers=self.http.headers,
            cookies={},
        )
        if response.status_code != 200:
            message = f"Error ({response.status_code}) getting pages"
            self.http.graylog.info(message)
            raise PageNotFoundError(message)
        return [Page.model_validate(p) for p in response.json()]

    def get_by_shared_id(self, shared_id: str) -> Page:
        response = self.http.request_adapter.get(
            url=f"{self.http.url}/api/pages",
            headers=self.http.headers,
            cookies={},
            params={"sharedId": shared_id},
        )
        if response.status_code != 200:
            message = f"Error ({response.status_code}) getting page {shared_id}"
            self.http.graylog.info(message)
            raise PageNotFoundError(message)
        data = response.json()
        if isinstance(data, list):
            if not data:
                raise PageNotFoundError(f"Page {shared_id} not found")
            return Page.model_validate(data[0])
        return Page.model_validate(data)

    def create(self, shared_id: str = "", entity_view: bool = False, locales: dict | None = None) -> dict:
        if locales is None:
            locales = {}
        payload = {
            "sharedId": shared_id,
            "entityView": entity_view,
            "locales": locales,
        }
        response = self.http.request_adapter.post(
            url=f"{self.http.url}/api/pages",
            headers=self.http.headers,
            cookies={},
            data=json.dumps(payload),
        )
        response.raise_for_status()
        return response.json()

    def delete(self, shared_id: str) -> None:
        response = self.http.request_adapter.delete(
            url=f"{self.http.url}/api/pages",
            headers=self.http.headers,
            params={"sharedId": shared_id},
            cookies={},
        )
        if response.status_code != 200:
            message = f"Error ({response.status_code}) deleting page {shared_id}"
            self.http.graylog.info(message)
            raise PageNotFoundError(message)
        self.http.graylog.info(f"Page deleted {shared_id}")
