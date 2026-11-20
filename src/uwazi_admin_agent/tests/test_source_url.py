"""Offline tests for Source Page URL resolution (pure)."""

from uwazi_admin_agent.domain.source_url import find_source_page_url


def test_single_link_value_is_unwrapped() -> None:
    raw = {"metadata": {"source_page_url": {"label": "Source", "url": "https://example.com/a"}}}
    assert find_source_page_url(raw) == "https://example.com/a"


def test_repeated_link_value_uses_first_url() -> None:
    raw = {
        "metadata": {
            "links": [
                {"label": "a", "url": "https://example.com/a"},
                {"label": "b", "url": "https://example.com/b"},
            ]
        }
    }
    assert find_source_page_url(raw) == "https://example.com/a"


def test_non_http_url_is_ignored() -> None:
    raw = {"metadata": {"source_page_url": {"label": "bad", "url": "file:///etc/passwd"}}}
    assert find_source_page_url(raw) is None


def test_entity_without_link_metadata_returns_none() -> None:
    assert find_source_page_url({"metadata": {"title": [{"value": "x"}]}}) is None
    assert find_source_page_url({"metadata": None}) is None
    assert find_source_page_url({}) is None
