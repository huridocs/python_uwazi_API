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

import socket
from typing import override
from urllib.parse import urljoin, urlparse

import httpx

from uwazi_admin_agent.configuration import URL_FETCH_MAX_BYTES, URL_FETCH_TIMEOUT_SECONDS
from uwazi_admin_agent.domain.url_fetch import is_allowed_url, is_public_ip
from uwazi_admin_agent.ports.url_fetcher_port import UrlFetcherPort, UrlFetchError

# A neutral User-Agent: some sites 403 a default httpx/requests agent. The
# fetched URL is operator-supplied public content, not a credentialed resource.
_USER_AGENT: str = "uwazi-admin-agent/1.0 (+source-page-extraction)"

# Redirect hops are followed MANUALLY so every hop is re-validated; a library
# follow would let ``public.example -> 169.254.169.254`` bypass the first check.
_MAX_REDIRECTS: int = 5


def _assert_public_target(url: str) -> None:
    """Resolve ``url`` and refuse it if ANY address is not globally routable.

    Called before every request (initial and each redirect). Checking every
    resolved address closes the simplest DNS trick of returning one public and
    one internal answer. A DNS-rebinding window between this check and the
    connect remains; closing it fully needs a transport pinned to the validated
    IP, which is out of scope for a trusted-operator deployment.
    """
    host = urlparse(url).hostname
    if not host:
        raise UrlFetchError(f"refused URL without a host: {url!r}")
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UrlFetchError(f"DNS failed for {host!r}: {exc}") from exc
    addresses = {str(info[4][0]) for info in infos}
    if not addresses or not all(is_public_ip(address) for address in addresses):
        raise UrlFetchError(f"refused non-public target {host!r}: {sorted(addresses)}")


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
            client
            if client is not None
            else httpx.Client(
                headers={"User-Agent": _USER_AGENT},
                follow_redirects=False,
                trust_env=False,
            )
        )

    @override
    async def fetch_text(self, url: str) -> str:
        chunks: list[bytes] = []
        total = 0
        try:
            for _ in range(_MAX_REDIRECTS + 1):
                if not is_allowed_url(url):
                    raise UrlFetchError(f"refused non-http(s) URL: {url!r}")
                _assert_public_target(url)
                with self._client.stream("GET", url, timeout=self._timeout, follow_redirects=False) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise UrlFetchError(f"redirect without a Location header: {url!r}")
                        url = urljoin(str(response.url), location)
                        continue
                    response.raise_for_status()
                    chunks.clear()
                    total = 0
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > self._max_bytes:
                            raise UrlFetchError(f"body exceeds {self._max_bytes} bytes: {url!r}")
                        chunks.append(chunk)
                    return b"".join(chunks).decode("utf-8", errors="replace")
            raise UrlFetchError(f"too many redirects (>{_MAX_REDIRECTS}): {url!r}")
        except UrlFetchError:
            raise
        except httpx.HTTPError as exc:
            raise UrlFetchError(f"fetch failed for {url!r}: {type(exc).__name__}: {exc}") from exc
