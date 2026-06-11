import json
from typing import Iterable

from uwazi_api.adapters.http_client_adapter import HttpClientAdapter
from uwazi_api.domain.exceptions import UploadError
from uwazi_api.domain.menu_link import MenuLink


class MenuLinksRepository:
    """Read and replace the Uwazi "Settings → Links" navigation list.

    Uwazi exposes the public-facing menu links at ``/api/settings/links``.
    The endpoint accepts a ``POST`` whose body is the full list of links and
    replaces whatever was stored. Reads come back as a plain JSON array of
    ``{title, type, url}`` objects.
    """

    def __init__(self, http_client: HttpClientAdapter):
        self.http = http_client

    def get_all(self) -> list[MenuLink]:
        response = self.http.request_adapter.get(
            url=f"{self.http.url}/api/settings/links",
            headers=self.http.headers,
            cookies={},
        )
        if response.status_code != 200:
            message = f"Error ({response.status_code}) getting menu links"
            self.http.graylog.info(message)
            raise UploadError(message)
        data = response.json()
        if not isinstance(data, list):
            return []
        return [self._to_model(entry) for entry in data]

    def replace_all(self, links: Iterable[MenuLink]) -> list[MenuLink]:
        payload = [self._to_payload(link) for link in links]
        response = self.http.request_adapter.post(
            url=f"{self.http.url}/api/settings/links",
            headers=self.http.headers,
            cookies={},
            data=json.dumps(payload),
        )
        if response.status_code != 200:
            message = f"Error ({response.status_code}) replacing menu links: {response.text}"
            self.http.graylog.info(message)
            raise UploadError(message)
        body = response.json()
        if not isinstance(body, list):
            return [self._to_model(entry) for entry in payload]
        return [self._to_model(entry) for entry in body]

    @staticmethod
    def _to_model(entry: dict) -> MenuLink:
        known = {"title", "type", "url"}
        extra = {k: v for k, v in entry.items() if k not in known}
        return MenuLink(
            title=entry.get("title", ""),
            type=entry.get("type", "link"),
            url=entry.get("url"),
            extra=extra,
        )

    @staticmethod
    def _to_payload(link: MenuLink) -> dict:
        payload: dict = {"title": link.title, "type": link.type}
        if link.url is not None:
            payload["url"] = link.url
        for key, value in link.extra.items():
            payload[key] = value
        return payload
