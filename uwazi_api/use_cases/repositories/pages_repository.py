import json
from typing import Optional

from uwazi_api.adapters.http_client_adapter import HttpClientAdapter
from uwazi_api.domain.exceptions import PageNotFoundError, UploadError
from uwazi_api.domain.page import Page


class PagesRepository:
    def __init__(self, http_client: HttpClientAdapter):
        self.http = http_client

    def get_all(self, language: str = "en") -> list[Page]:
        response = self.http.request_adapter.get(
            url=f"{self.http.url}/api/pages",
            headers=self.http.headers,
            cookies={"locale": language},
        )
        if response.status_code != 200:
            message = f"Error ({response.status_code}) getting pages"
            self.http.graylog.info(message)
            raise PageNotFoundError(message)
        return [Page.model_validate(p) for p in response.json()]

    def get_by_shared_id(self, shared_id: str, language: str = "en") -> Page:
        response = self.http.request_adapter.get(
            url=f"{self.http.url}/api/pages",
            headers=self.http.headers,
            cookies={"locale": language},
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

    def create(
        self,
        title: str,
        content: str = "",
        script: Optional[str] = None,
        css: Optional[str] = None,
        entity_view: bool = False,
        language: str = "en",
    ) -> Page:
        metadata: dict[str, str] = {"content": content or ""}
        if script:
            metadata["script"] = script
        if css:
            metadata["css"] = css
        payload = {
            "title": title,
            "language": language,
            "entityView": entity_view,
            "metadata": metadata,
        }
        response = self.http.request_adapter.post(
            url=f"{self.http.url}/api/pages",
            headers=self.http.headers,
            cookies={"locale": language},
            data=json.dumps(payload),
        )
        if response.status_code != 200:
            message = f"Error ({response.status_code}) creating page '{title}': {response.text}"
            self.http.graylog.info(message)
            raise UploadError(message)
        return Page.model_validate(response.json())

    def update(self, page: Page) -> Page:
        if not page.id or not page.shared_id:
            raise UploadError("Updating a page requires both its '_id' and 'sharedId'.")
        language = page.language or "en"
        # The /api/pages schema rejects additional properties, so only send
        # the fields it accepts (not the model defaults like draft/releases).
        payload = {
            "_id": page.id,
            "sharedId": page.shared_id,
            "title": page.title,
            "language": language,
            "entityView": page.entity_view,
            "metadata": page.metadata or {},
        }
        response = self.http.request_adapter.post(
            url=f"{self.http.url}/api/pages",
            headers=self.http.headers,
            cookies={"locale": language},
            data=json.dumps(payload),
        )
        if response.status_code != 200:
            message = f"Error ({response.status_code}) updating page {page.shared_id}: {response.text}"
            self.http.graylog.info(message)
            raise UploadError(message)
        return Page.model_validate(response.json())

    def delete(self, shared_id: str, language: str = "en") -> None:
        response = self.http.request_adapter.delete(
            url=f"{self.http.url}/api/pages",
            headers=self.http.headers,
            params={"sharedId": shared_id},
            cookies={"locale": language},
        )
        if response.status_code != 200:
            message = f"Error ({response.status_code}) deleting page {shared_id}"
            self.http.graylog.info(message)
            raise PageNotFoundError(message)
        self.http.graylog.info(f"Page deleted {shared_id}")
