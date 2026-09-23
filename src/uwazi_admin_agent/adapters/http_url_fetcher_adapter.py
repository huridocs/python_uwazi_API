"""``httpx``-backed external URL fetch (source-page HTML extraction).

Fetches an operator-supplied ``http(s)`` URL and returns its body decoded as
UTF-8 text, fenced by the scheme allow-list (:func:`is_allowed_url`), a timeout,
and a byte cap (:data:`URL_FETCH_MAX_BYTES`). Any failure — disallowed scheme,
network error, non-2xx status, oversized body — raises :class:`UrlFetchError` so
the exec helper can degrade to ``None`` and the authoring tool to an error
string, never crashing a bulk run.

Async by signature (the port is async); the underlying ``httpx`` calls are
synchronous, matching the other repository adapters. ``httpx`` (already a
project dependency, fully typed) is used instead of ``requests`` so the adapter
stays type-checked without a separate stub package. Not unit-tested (I/O) —
validated live alongside the rest of the harness.
"""

from __future__ import annotations

from typing import override

import httpx

from uwazi_admin_agent.configuration import URL_FETCH_MAX_BYTES, URL_FETCH_TIMEOUT_SECONDS
from uwazi_admin_agent.domain.url_fetch import is_allowed_url
from uwazi_admin_agent.ports.url_fetcher_port import UrlFetcherPort, UrlFetchError

# A neutral User-Agent: some sites 403 a default httpx/requests agent. The
# fetched URL is operator-supplied public content, not a credentialed resource.
_USER_AGENT: str = "uwazi-admin-agent/1.0 (+source-page-extraction)"


class HttpUrlFetcherAdapter(UrlFetcherPort):
    """Fetch an http(s) URL's body as UTF-8 text via ``httpx`` (§ source-url)."""

    def __init__(
        self,
        timeout: float = URL_FETCH_TIMEOUT_SECONDS,
        max_bytes: int = URL_FETCH_MAX_BYTES,
        client: httpx.Client | None = None,
    ) -> None:
        self._timeout = timeout
        self._max_bytes = max_bytes
        # A shared Client reuses the connection pool across fetches; injectable
        # so a caller can pass a pre-configured client (proxy, headers, etc.).
        self._client = (
            client if client is not None else httpx.Client(headers={"User-Agent": _USER_AGENT}, follow_redirects=True)
        )

    @override
    async def fetch_text(self, url: str) -> str:
        if not is_allowed_url(url):
            raise UrlFetchError(f"refused non-http(s) URL: {url!r}")
        chunks: list[bytes] = []
        total = 0
        try:
            with self._client.stream("GET", url, timeout=self._timeout) as response:
                response.raise_for_status()
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > self._max_bytes:
                        raise UrlFetchError(f"body exceeds {self._max_bytes} bytes: {url!r}")
                    chunks.append(chunk)
        except UrlFetchError:
            raise
        except httpx.HTTPError as exc:
            raise UrlFetchError(f"fetch failed for {url!r}: {type(exc).__name__}: {exc}") from exc
        return b"".join(chunks).decode("utf-8", errors="replace")
