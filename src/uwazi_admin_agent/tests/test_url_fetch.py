"""Isolated unit tests for external URL fetching (source-page extraction).

Per ``AGENTS.md``: no mocks/stubs, no network, no running Uwazi instance. The
pure helpers (``is_allowed_url`` / ``is_public_ip`` / ``truncate_url_text``) are
tested with literals; the namespace helpers are tested with real in-memory port
implementations (no I/O) and their unwired ``RuntimeError`` stubs.
"""

import asyncio

import pytest

from uwazi_admin_agent.domain.url_fetch import is_allowed_url, is_public_ip, truncate_url_text
from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort
from uwazi_admin_agent.ports.url_fetcher_port import UrlFetcherPort, UrlFetchError
from uwazi_admin_agent.use_cases.script_exec_namespace import (
    _build_get_url_text_real_helper,
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


class _InMemoryEntityRepository(EntityRepositoryPort):
    """Real in-memory entity source (no I/O): maps shared_id -> raw entity.

    Only the read path ``get_url_text`` needs is implemented; the mutating
    methods belong to the backup/revert paths and are unused here.
    """

    def __init__(self, raws: dict[str, dict]) -> None:
        self._raws = raws

    async def get_raw_by_shared_id(self, shared_id: str, language: str | None = None) -> dict:
        del language
        if shared_id not in self._raws:
            raise KeyError(shared_id)
        return self._raws[shared_id]

    async def get_raw_by_internal_id(self, internal_id: str) -> dict:
        raise NotImplementedError

    async def save_raw(self, raw: dict) -> None:
        raise NotImplementedError

    async def create_raw(self, raw: dict) -> str:
        raise NotImplementedError

    async def delete_by_shared_id(self, shared_id: str) -> None:
        raise NotImplementedError


def _repo_with_url(shared_id: str, url: str) -> _InMemoryEntityRepository:
    return _InMemoryEntityRepository({shared_id: {"metadata": {"source_page_url": {"label": "Source", "url": url}}}})


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


def test_is_allowed_url_rejects_embedded_userinfo() -> None:
    assert is_allowed_url("http://user:pass@example.com/") is False
    assert is_allowed_url("http://user@example.com/") is False


# --- pure SSRF address check --------------------------------------------------


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.0.0.1", "172.16.0.1", "192.168.1.1", "169.254.169.254", "::1", "0.0.0.0", "::ffff:127.0.0.1"],
)
def test_is_public_ip_rejects_internal_targets(address: str) -> None:
    assert is_public_ip(address) is False


def test_is_public_ip_accepts_global_addresses() -> None:
    assert is_public_ip("8.8.8.8") is True
    assert is_public_ip("2001:4860:4860::8888") is True


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


def test_get_url_text_real_helper_raises_when_unwired() -> None:
    get_url_text = _build_get_url_text_real_helper(None, None, asyncio.new_event_loop(), "en")
    with pytest.raises(RuntimeError):
        get_url_text("entity-1")


def test_get_url_text_real_helper_fetches_the_entitys_source_url() -> None:
    loop = asyncio.new_event_loop()
    get_url_text = _build_get_url_text_real_helper(
        _repo_with_url("entity-1", "https://example.com"),
        _InMemoryUrlFetcher({"https://example.com": "<html>x</html>"}),
        loop,
        "en",
    )
    assert get_url_text("entity-1") == "<html>x</html>"


def test_get_url_text_real_helper_returns_none_on_fetch_failure() -> None:
    loop = asyncio.new_event_loop()
    get_url_text = _build_get_url_text_real_helper(
        _repo_with_url("entity-1", "https://missing.example.com"),
        _InMemoryUrlFetcher({}),
        loop,
        "en",
    )
    assert get_url_text("entity-1") is None


def test_get_url_text_real_helper_returns_none_when_entity_has_no_link() -> None:
    loop = asyncio.new_event_loop()
    repo = _InMemoryEntityRepository({"entity-1": {"metadata": {"title": [{"value": "x"}]}}})
    get_url_text = _build_get_url_text_real_helper(repo, _InMemoryUrlFetcher({"https://example.com": "unused"}), loop, "en")
    assert get_url_text("entity-1") is None


# --- namespace binding --------------------------------------------------------


def test_dry_run_namespace_binds_get_url_text_from_wired_ports() -> None:
    ns = build_dry_run_namespace(
        entity_api=None,
        loop=asyncio.new_event_loop(),
        file_repository=None,
        default_language="en",
        dry_run_records=[],
        entity_repository=_repo_with_url("entity-1", "https://example.com"),
        url_fetcher=_InMemoryUrlFetcher({"https://example.com": "<html>ok</html>"}),
    )
    assert ns["get_url_text"]("entity-1") == "<html>ok</html>"
    assert ns["get_url_text"]("unknown-entity") is None


def test_dry_run_namespace_get_url_text_is_unwired_stub_when_ports_missing() -> None:
    ns = build_dry_run_namespace(
        entity_api=None,
        loop=asyncio.new_event_loop(),
        file_repository=None,
        default_language="en",
        dry_run_records=[],
        entity_repository=None,
    )
    with pytest.raises(RuntimeError):
        ns["get_url_text"]("entity-1")
