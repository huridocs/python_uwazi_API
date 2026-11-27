"""Unit tests for the per-instance URL helpers (pure, offline, deterministic)."""

from __future__ import annotations

from uwazi_admin_agent.drivers.links import (
    instance_token,
    instance_tokens,
    login_link,
    match_instance,
    page_link,
    path_tokens,
    startup_login_links,
)

CONFIGURED = ["http://localhost:3000", "https://www.girlsrightsplatform.org", "https://www.upr-info.org"]


def test_instance_tokens_lists_short_forms_in_preference_order() -> None:
    assert instance_tokens("https://global-rep.uwazi.io") == [
        "global-rep",
        "global-rep.uwazi.io",
        "https:global-rep.uwazi.io",
        "https://global-rep.uwazi.io",
    ]


def test_instance_tokens_covers_the_host_without_port() -> None:
    assert instance_tokens("http://localhost:3000") == [
        "localhost",
        "localhost:3000",
        "http:localhost:3000",
        "http://localhost:3000",
    ]


def test_instance_tokens_skips_a_www_prefix() -> None:
    assert instance_tokens("https://www.girlsrightsplatform.org") == [
        "girlsrightsplatform",
        "girlsrightsplatform.org",
        "www.girlsrightsplatform.org",
        "www",
        "https:www.girlsrightsplatform.org",
        "https://www.girlsrightsplatform.org",
    ]


def test_instance_tokens_reads_a_scheme_less_host() -> None:
    assert "upr-info" in instance_tokens("upr-info.org")
    assert "upr-info.org" in instance_tokens("upr-info.org")


def test_match_instance_accepts_the_documented_spellings() -> None:
    assert match_instance("upr-info", CONFIGURED) == "https://www.upr-info.org"
    assert match_instance("upr-info.org", CONFIGURED) == "https://www.upr-info.org"
    assert match_instance("www.upr-info.org", CONFIGURED) == "https://www.upr-info.org"
    assert match_instance("https://www.upr-info.org/", CONFIGURED) == "https://www.upr-info.org"
    assert match_instance("localhost", CONFIGURED) == "http://localhost:3000"
    assert match_instance("localhost:3000", CONFIGURED) == "http://localhost:3000"
    assert match_instance("girlsrightsplatform.org", CONFIGURED) == "https://www.girlsrightsplatform.org"


def test_match_instance_refuses_unknown_ambiguous_and_empty_tokens() -> None:
    assert match_instance("nope", CONFIGURED) is None
    assert match_instance("www", CONFIGURED) is None  # two configured www hosts
    assert match_instance("", CONFIGURED) is None


def test_path_tokens_are_single_segment_and_unambiguous() -> None:
    tokens = path_tokens("https://www.upr-info.org", CONFIGURED)

    assert tokens[:2] == ["upr-info", "upr-info.org"]
    assert "www" not in tokens
    assert all("/" not in token for token in tokens)
    assert path_tokens("http://localhost:3000", CONFIGURED)[:2] == ["localhost", "localhost:3000"]


def test_instance_token_is_the_canonical_short_form() -> None:
    assert instance_token("https://www.upr-info.org", CONFIGURED) == "upr-info"
    assert instance_token("http://localhost:3000", CONFIGURED) == "localhost"


def test_login_link_is_an_instance_path() -> None:
    assert login_link("", "upr-info", "admin") == "/upr-info?user=admin"
    assert login_link("", "upr-info", "") == "/upr-info"
    assert login_link("https://admin.example.org/", "upr-info", "") == "https://admin.example.org/upr-info"


def test_startup_login_links_names_every_configured_instance() -> None:
    assert startup_login_links("http://127.0.0.1:5055", CONFIGURED, "admin") == [
        "http://127.0.0.1:5055/localhost?user=admin",
        "http://127.0.0.1:5055/girlsrightsplatform?user=admin",
        "http://127.0.0.1:5055/upr-info?user=admin",
    ]


def test_page_link_prefixes_the_instance_path() -> None:
    assert page_link("/app", "https://www.upr-info.org", CONFIGURED) == "/upr-info/app"
    assert page_link("/review/run-1", "https://www.upr-info.org", CONFIGURED) == "/upr-info/review/run-1"
    assert page_link("/task/task-1", "https://www.upr-info.org", CONFIGURED) == "/upr-info/task/task-1"
    assert page_link("/", "https://www.upr-info.org", CONFIGURED) == "/upr-info"
    assert page_link("/app", "", CONFIGURED) == "/app"


def test_no_configured_instance_yields_no_links() -> None:
    assert startup_login_links("http://127.0.0.1:5055", [], "admin") == []
    assert instance_token("", CONFIGURED) == ""
