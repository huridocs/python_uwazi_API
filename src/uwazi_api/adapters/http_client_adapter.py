import logging
from typing import Optional

from requests.cookies import merge_cookies

from uwazi_api.adapters.request_retry import requests_retry_session
from uwazi_api.domain.exceptions import AuthenticationError
from uwazi_api.iso_639_choices import iso_639_choices
from uwazi_api.ports.http_port import HttpClientPort


class HttpClientAdapter(HttpClientPort):
    def __init__(self, url: str, user: Optional[str] = None, password: Optional[str] = None, token: Optional[str] = None):
        if not url:
            raise ValueError("URL is required and cannot be None or empty")
        url = url.rstrip("/")
        for language in iso_639_choices:
            if url[-3:] == f"/{language[0]}":
                url = url[:-3]
        self.url = url
        self.user = user
        self.password = password
        self.token = token
        self.request_adapter = requests_retry_session()
        self.headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/json",
        }
        self.graylog = logging.getLogger("graylog")
        self.connect_sid = self._get_connect_sid() if user and password else None

    def post_multipart(
        self,
        url: str,
        data: Optional[dict] = None,
        files: Optional[list] = None,
        cookies: Optional[dict] = None,
    ):
        """Send a multipart/form-data POST request.

        Strips ``Content-Type`` from the default headers so ``requests``
        can set the correct ``multipart/form-data; boundary=...`` header.
        """
        headers = {k: v for k, v in self.headers.items() if k != "Content-Type"}
        return self.request_adapter.post(
            url,
            headers=headers,
            data=data,
            files=files,
            cookies=cookies,
        )

    def post_json(
        self,
        url: str,
        json: dict,
        cookies: Optional[dict] = None,
    ):
        """Send a JSON POST request.

        Sends the payload as ``application/json`` using the default headers.
        """
        return self.request_adapter.post(
            url,
            headers=self.headers,
            json=json,
            cookies=cookies,
        )

    def _get_connect_sid(self) -> str:
        # Login is a handshake, not a maintenance-window call: use a session
        # WITHOUT the exponential-backoff retry policy. That policy (up to
        # ~10 minutes of sleeps) exists to ride out 502s on API calls; on
        # login it only turns a wrong scheme/URL into a multi-minute stall of
        # whatever thread is logging in. Scheme fallback lives in the agent's
        # ``login_url_candidates``, so a failed attempt here must fail fast.
        login_session = requests_retry_session(retries=0)
        try:
            response = login_session.post(
                f"{self.url}/api/login",
                headers=self.headers,
                json={"username": self.user, "password": self.password, **({"token": self.token} if self.token else {})},
            )
            if response.status_code != 200:
                raise AuthenticationError(f"Login failed: {response.status_code}")
            cookie = response.cookies.get("connect.sid")
            if not cookie:
                raise AuthenticationError("No connect.sid cookie received")
            # Carry the SERVER-parsed cookies over as-is. Re-building the
            # session cookie with ``cookies.set(domain=hostname)`` marks it
            # ``domain_specified``, and http.cookiejar then never sends it to a
            # bare host such as localhost (the domain would have to end in
            # ".localhost"). The session would silently stop authenticating:
            # Uwazi's reads are public, so only the first write reveals it —
            # a 401 on every entity create/update.
            merge_cookies(self.request_adapter.cookies, login_session.cookies)
        finally:
            login_session.close()
        self.graylog.info(f"Login into {self.url}: {response.status_code}")
        return cookie
