"""Isolated tests for the login base-URL candidates (pure function)."""

from uwazi_agent.drivers.rest.models.uwazi_credentials import login_url_candidates


def test_http_url_tries_https_first_then_the_given_http() -> None:
    # The local dev case: port 3000 is plain HTTP, so the https probe must be
    # followed by the working http URL — that fallback used to cost ~10 min.
    assert login_url_candidates("http://localhost:3000") == [
        "https://localhost:3000",
        "http://localhost:3000",
    ]


def test_explicit_https_never_downgrades() -> None:
    assert login_url_candidates("https://global-rep.uwazi.io") == ["https://global-rep.uwazi.io"]


def test_scheme_less_url_offers_both_schemes_https_first() -> None:
    assert login_url_candidates("localhost:3000") == ["https://localhost:3000", "http://localhost:3000"]


def test_host_port_and_path_are_preserved_verbatim() -> None:
    assert login_url_candidates("http://uwazi.internal:8080/sub/en") == [
        "https://uwazi.internal:8080/sub/en",
        "http://uwazi.internal:8080/sub/en",
    ]


def test_surrounding_whitespace_is_stripped() -> None:
    assert login_url_candidates("  http://localhost:3000  ") == [
        "https://localhost:3000",
        "http://localhost:3000",
    ]


def test_empty_url_yields_a_single_empty_candidate() -> None:
    # The adapter raises its "URL is required" ValueError on that candidate,
    # which the factory reports instead of silently trying other schemes.
    assert login_url_candidates("") == [""]
    assert login_url_candidates("   ") == [""]
