"""Unit tests for the invite-link helpers (pure, offline, deterministic)."""

from __future__ import annotations

from uwazi_admin_agent.drivers.links import (
    AMBIGUOUS_INSTANCE_MESSAGE,
    MISSING_INSTANCE_MESSAGE,
    UNMATCHED_INSTANCE_MESSAGE,
    instance_token,
    instance_tokens,
    login_link,
    login_links,
    match_instance,
    resolve_login_target,
    startup_login_url,
)

CONFIGURED = ["https://global-rep.uwazi.io", "http://localhost:3000"]


def test_instance_tokens_lists_short_forms_in_preference_order() -> None:
    assert instance_tokens("https://global-rep.uwazi.io") == [
        "global-rep",
        "global-rep.uwazi.io",
        "https://global-rep.uwazi.io",
    ]


def test_instance_tokens_deduplicates_a_single_label_host() -> None:
    assert instance_tokens("http://localhost:3000") == [
        "localhost:3000",
        "http://localhost:3000",
    ]


def test_match_instance_accepts_the_short_form() -> None:
    assert match_instance("global-rep", CONFIGURED) == ("https://global-rep.uwazi.io", None)


def test_match_instance_accepts_the_full_url_with_a_trailing_slash() -> None:
    assert match_instance("https://global-rep.uwazi.io/", CONFIGURED) == (
        "https://global-rep.uwazi.io",
        None,
    )


def test_match_instance_accepts_a_differently_cased_host() -> None:
    assert match_instance("HTTPS://Global-Rep.Uwazi.IO", CONFIGURED) == (
        "https://global-rep.uwazi.io",
        None,
    )


def test_match_instance_uses_the_port_to_disambiguate_localhost() -> None:
    assert match_instance("localhost:3000", CONFIGURED) == ("http://localhost:3000", None)


def test_match_instance_rejects_an_unknown_token() -> None:
    assert match_instance("nope", CONFIGURED) == (None, UNMATCHED_INSTANCE_MESSAGE)


def test_match_instance_reports_an_ambiguous_token() -> None:
    configured = ["https://global-rep.uwazi.io", "https://global-rep.staging.uwazi.io"]
    matched, error = match_instance("global-rep", configured)
    assert matched is None
    assert error == AMBIGUOUS_INSTANCE_MESSAGE


def test_instance_token_avoids_a_shared_first_label() -> None:
    configured = ["https://global-rep.uwazi.io", "https://global-rep.staging.uwazi.io"]
    token = instance_token("https://global-rep.uwazi.io", configured)
    assert token != "global-rep"
    assert match_instance(token, configured) == ("https://global-rep.uwazi.io", None)


def test_instance_token_uses_the_short_form_when_unambiguous() -> None:
    assert instance_token("https://b.example", ["https://a.example", "https://b.example"]) == "b"


def test_login_link_encodes_the_instance_and_user() -> None:
    assert login_link("https://admin.example.org/", "global-rep", "admin") == (
        "https://admin.example.org/?instance=global-rep&user=admin"
    )


def test_login_link_drops_an_empty_user_and_encodes_a_full_url() -> None:
    assert login_link("", "https://a.example", "") == "/?instance=https%3A%2F%2Fa.example"


def test_login_link_keeps_a_relative_form_without_a_base_url() -> None:
    assert login_link("", "a.example", "") == "/?instance=a.example"


def test_resolve_login_target_on_a_bare_path_requires_the_instance() -> None:
    assert resolve_login_target("", "", CONFIGURED) == ("", "", MISSING_INSTANCE_MESSAGE)


def test_resolve_login_target_treats_a_blank_instance_as_missing() -> None:
    assert resolve_login_target("   ", "", CONFIGURED) == ("", "", MISSING_INSTANCE_MESSAGE)


def test_resolve_login_target_preselects_the_instance_and_the_user() -> None:
    assert resolve_login_target("global-rep", "  bob ", CONFIGURED) == (
        "https://global-rep.uwazi.io",
        "bob",
        None,
    )


def test_resolve_login_target_keeps_the_user_without_an_instance() -> None:
    assert resolve_login_target("", " bobby ", CONFIGURED) == ("", "bobby", MISSING_INSTANCE_MESSAGE)


def test_resolve_login_target_keeps_an_empty_user() -> None:
    assert resolve_login_target("global-rep", "", CONFIGURED) == (
        "https://global-rep.uwazi.io",
        "",
        None,
    )


def test_resolve_login_target_refuses_an_unconfigured_instance() -> None:
    assert resolve_login_target("http://169.254.169.254", "admin", CONFIGURED) == (
        "",
        "admin",
        UNMATCHED_INSTANCE_MESSAGE,
    )


def test_resolve_login_target_without_configured_instances() -> None:
    assert resolve_login_target("x", "u", []) == ("", "u", None)


def test_login_links_render_one_link_per_configured_instance() -> None:
    links = login_links(
        ["https://a.example", "https://b.example"],
        "https://admin.example",
        "admin",
        "b.example",
    )
    assert links == ["https://admin.example/?instance=b&user=admin"]


def test_login_links_without_a_user_omit_the_user_parameter() -> None:
    links = login_links(CONFIGURED, "https://admin.example", "")
    assert links == [
        "https://admin.example/?instance=global-rep",
        "https://admin.example/?instance=localhost%3A3000",
    ]


def test_login_links_for_an_unmatched_instance_are_empty() -> None:
    assert login_links(CONFIGURED, "https://admin.example", "admin", "nope") == []


def test_startup_login_url_is_none_without_configured_instances() -> None:
    assert startup_login_url([], "", 5055) is None


def test_startup_login_url_falls_back_to_localhost() -> None:
    assert startup_login_url(["https://global-rep.uwazi.io"], "", 5055) == ("http://localhost:5055/?instance=global-rep")


def test_startup_login_url_uses_the_public_base_url() -> None:
    assert startup_login_url(["https://global-rep.uwazi.io"], "https://admin.example/", 5055) == (
        "https://admin.example/?instance=global-rep"
    )
