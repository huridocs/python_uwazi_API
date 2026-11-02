"""Isolated unit tests for the instance registry (pure, offline).

No I/O, no environment reads, no mocks: the parsing/keys/slugs are pure
functions over literal strings.
"""

from __future__ import annotations

from uwazi_admin_agent.domain.instance import instance_key, instance_slug, parse_instance_urls


def test_parse_instance_urls_single_url() -> None:
    assert parse_instance_urls("http://a") == ["http://a"]


def test_parse_instance_urls_splits_and_strips() -> None:
    assert parse_instance_urls("http://a, http://b ,") == ["http://a", "http://b"]


def test_parse_instance_urls_deduplicates_keeping_the_first_occurrence() -> None:
    assert parse_instance_urls("http://a,http://a") == ["http://a"]


def test_parse_instance_urls_empty_and_none() -> None:
    assert parse_instance_urls("") == []
    assert parse_instance_urls(None) == []


def test_instance_key_ignores_a_trailing_slash() -> None:
    assert instance_key("https://x.io/") == instance_key("https://x.io")


def test_instance_key_differs_per_instance() -> None:
    assert instance_key("http://a") != instance_key("http://b")


def test_instance_key_is_sixteen_hex_characters() -> None:
    key = instance_key("http://a")
    assert len(key) == 16
    assert all(char in "0123456789abcdef" for char in key)


def test_instance_slug_is_filesystem_safe() -> None:
    slug = instance_slug("http://localhost:3000")
    assert "/" not in slug
    assert ":" not in slug
    assert slug == slug.strip()


def test_instance_slug_is_stable() -> None:
    assert instance_slug("https://x.io/tenant") == instance_slug("https://x.io/tenant")


def test_instance_slug_differs_between_hosts() -> None:
    assert instance_slug("http://a") != instance_slug("http://b")


def test_instance_slug_ignores_a_trailing_slash() -> None:
    assert instance_slug("http://a/") == instance_slug("http://a")
