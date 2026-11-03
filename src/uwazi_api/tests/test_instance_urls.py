"""Isolated tests for the shared ``UWAZI_URL`` parser (pure, offline).

One URL or a comma-separated list must resolve to the same single-instance
answer everywhere; these are literal inputs, no environment, no network.
"""

from __future__ import annotations

from uwazi_api.instance_urls import first_instance_url, parse_instance_urls


def test_a_single_url_is_a_one_item_list() -> None:
    assert parse_instance_urls("http://localhost:3000") == ["http://localhost:3000"]


def test_a_comma_separated_list_is_split_and_stripped() -> None:
    assert parse_instance_urls("http://localhost:3000, https://uwazi.example.org ,") == [
        "http://localhost:3000",
        "https://uwazi.example.org",
    ]


def test_duplicates_keep_the_first_occurrence() -> None:
    assert parse_instance_urls("http://a,http://b,http://a") == ["http://a", "http://b"]


def test_empty_inputs_yield_no_urls() -> None:
    assert parse_instance_urls("") == []
    assert parse_instance_urls(" , ,") == []
    assert parse_instance_urls(None) == []


def test_first_instance_url_returns_the_first_entry_of_either_shape() -> None:
    assert first_instance_url("http://a") == "http://a"
    assert first_instance_url("http://a,https://b") == "http://a"


def test_first_instance_url_is_none_when_unset() -> None:
    assert first_instance_url(None) is None
    assert first_instance_url("") is None
    assert first_instance_url("  ") is None


def test_the_first_entry_is_never_a_glued_list() -> None:
    """The bug this guards: ``http://a,https://b`` reaching requests as a URL."""
    resolved = first_instance_url("http://localhost:3000,https://localhost:3000")
    assert "," not in resolved
