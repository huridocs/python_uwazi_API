"""Isolated unit tests for external URL fetching (source-page extraction).

Per ``AGENTS.md``: no mocks/stubs, no network, no running Uwazi instance. The
pure helpers (``is_allowed_url`` / ``truncate_url_text``) are tested with
literals; the namespace helpers are tested with a real in-memory
``UrlFetcherPort`` (no I/O) and their unwired ``RuntimeError`` stubs.
"""

import asyncio

import pytest

from uwazi_admin_agent.domain.url_fetch import is_allowed_url, truncate_url_text
from uwazi_admin_agent.ports.url_fetcher_port import UrlFetcherPort, UrlFetchError
from uwazi_admin_agent.use_cases.script_exec_namespace import (
    _build_get_url_text_real_helper,
    _get_url_text_noop,
    build_dry_run_namespace,
)


class _InMemoryUrlFetcher(UrlFetcherPort):
    """Real in-memory fetcher (no I/O): serves pages from a dict, errors on a miss."""

    def __init__(self, pages: dict[str, str]) -> None:
        self._pages = pages

    async def fetch_text(self, url: str) -> str:
        if url not in self._pages:
            raise UrlFetchError(f"missing {url}")
        return self._pages[url]


# --- pure scheme allow-list ---------------------------------------------------


def test_is_allowed_url_accepts_http_and_https_with_host() -> None:
    assert is_allowed_url("https://example.com/page") is True
    assert is_allowed_url("http://example.com") is True


def test_is_allowed_url_rejects_non_http_schemes() -> None:
    assert is_allowed_url("ftp://example.com") is False
    assert is_allowed_url("file:///etc/passwd") is False
    assert is_allowed_url("javascript:alert(1)") is False
    assert is_allowed_url("gopher://example.com") is False


def test_is_allowed_url_rejects_missing_scheme_or_host() -> None:
    assert is_allowed_url("") is False
    assert is_allowed_url("example.com") is False
    assert is_allowed_url("https://") is False


# --- pure truncation ----------------------------------------------------------


def test_truncate_url_text_leaves_short_text_unchanged() -> None:
    assert truncate_url_text("short", max_chars=100) == "short"


def test_truncate_url_text_keeps_head_and_tail() -> None:
    text = "a" * 100 + "TAIL"
    out = truncate_url_text(text, max_chars=60, tail_chars=10)
    assert out.startswith("a" * 50)  # head = 60 - 10
    assert "[...middle truncated...]" in out
    assert out.endswith("TAIL[truncated]")
    assert len(out) > 60  # marker + flag exceed the cap; content is bounded


# --- namespace helpers --------------------------------------------------------


def test_get_url_text_noop_returns_none() -> None:
    get_url_text = _get_url_text_noop()
    assert get_url_text("https://example.com") is None


def test_get_url_text_real_helper_raises_when_unwired() -> None:
    get_url_text = _build_get_url_text_real_helper(None, asyncio.new_event_loop())
    with pytest.raises(RuntimeError):
        get_url_text("https://example.com")


def test_get_url_text_real_helper_fetches_text() -> None:
    loop = asyncio.new_event_loop()
    get_url_text = _build_get_url_text_real_helper(_InMemoryUrlFetcher({"https://example.com": "<html>x</html>"}), loop)
    assert get_url_text("https://example.com") == "<html>x</html>"


def test_get_url_text_real_helper_returns_none_on_fetch_failure() -> None:
    loop = asyncio.new_event_loop()
    get_url_text = _build_get_url_text_real_helper(_InMemoryUrlFetcher({}), loop)
    assert get_url_text("https://missing.example.com") is None


# --- namespace binding --------------------------------------------------------


def test_dry_run_namespace_binds_get_url_text_from_wired_port() -> None:
    ns = build_dry_run_namespace(
        entity_api=None,
        loop=asyncio.new_event_loop(),
        file_repository=None,
        default_language="en",
        dry_run_records=[],
        entity_repository=None,
        url_fetcher=_InMemoryUrlFetcher({"https://example.com": "<html>ok</html>"}),
    )
    assert ns["get_url_text"]("https://example.com") == "<html>ok</html>"
    assert ns["get_url_text"]("https://missing.example.com") is None


def test_dry_run_namespace_get_url_text_is_unwired_stub_when_port_missing() -> None:
    ns = build_dry_run_namespace(
        entity_api=None,
        loop=asyncio.new_event_loop(),
        file_repository=None,
        default_language="en",
        dry_run_records=[],
        entity_repository=None,
    )
    with pytest.raises(RuntimeError):
        ns["get_url_text"]("https://example.com")
