import logging
from typing import Optional
from urllib.parse import urlparse

from uwazi_api.domain.exceptions import AuthenticationError
from uwazi_api.iso_639_choices import iso_639_choices
from uwazi_api.ports.http_port import HttpClientPort
from uwazi_api.adapters.request_retry import requests_retry_session


class HttpClientAdapter(HttpClientPort):
    def __init__(self, url: str, user: Optional[str] = None, password: Optional[str] = None):
        if not url:
            raise ValueError("URL is required and cannot be None or empty")
        url = url.rstrip("/")
        for language in iso_639_choices:
            if url[-3:] == f"/{language[0]}":
                url = url[:-3]
        self.url = url
        self.user = user
        self.password = password
        self.request_adapter = requests_retry_session()
        self.headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/json",
        }
        self.graylog = logging.getLogger("graylog")
        self.connect_sid = self._get_connect_sid() if user and password else None

    def _get_connect_sid(self) -> str:
        response = self.request_adapter.post(
            f"{self.url}/api/login",
            headers=self.headers,
            json={"username": self.user, "password": self.password},
        )
        if response.status_code != 200:
            raise AuthenticationError(f"Login failed: {response.status_code}")
        self.graylog.info(f"Login into {self.url}: {response.status_code}")
        cookie = response.cookies.get("connect.sid")
        if not cookie:
            raise AuthenticationError("No connect.sid cookie received")
        parsed = urlparse(self.url)
        self.request_adapter.cookies.set("connect.sid", cookie, domain=parsed.hostname, path="/")
        return cookie
