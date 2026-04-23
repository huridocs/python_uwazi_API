import logging
from typing import Optional

from uwazi_api.domain.exceptions import AuthenticationError
from uwazi_api.iso_639_choices import iso_639_choices
from uwazi_api.ports.http_port import HttpClientPort
from uwazi_api.adapters.request_retry import requests_retry_session


class HttpClientAdapter(HttpClientPort):
    def __init__(self, url: str, user: str, password: str):
        url = url if url[-1] != "/" else url[:-1]
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
        self.connect_sid = self._get_connect_sid()

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
        return cookie
