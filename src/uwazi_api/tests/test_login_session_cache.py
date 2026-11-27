"""Isolated tests for the process-local login-cookie cache.

The cache is what lets a 2FA account act after its 30-second code expired: the
first adapter logs in (password + code) and every later adapter for the SAME
credentials reuses the ``connect.sid`` cookie instead of re-running
``/api/login``. These tests use real values and a real adapter whose cache hit
does no I/O.
"""

import pytest
from requests import Request
from requests.cookies import get_cookie_header

from uwazi_api.adapters.http_client_adapter import HttpClientAdapter
from uwazi_api.adapters.login_session_cache import (
    cached_login_session,
    clear_login_sessions,
    remember_login_session,
)

URL = "http://localhost:3000"


@pytest.fixture(autouse=True)
def _empty_cache():
    clear_login_sessions()
    try:
        yield
    finally:
        clear_login_sessions()


def test_a_remembered_session_is_returned_for_the_same_credentials() -> None:
    remember_login_session(URL, "u", "p", "s%3Aabc.def")

    assert cached_login_session(URL, "u", "p") == "s%3Aabc.def"


def test_a_different_password_user_or_url_misses_the_cache() -> None:
    remember_login_session(URL, "u", "p", "s%3Aabc.def")

    assert cached_login_session(URL, "u", "other") is None
    assert cached_login_session(URL, "other", "p") is None
    assert cached_login_session("http://uwazi.example.org", "u", "p") is None


def test_a_trailing_slash_does_not_miss_the_cache() -> None:
    remember_login_session(f"{URL}/", "u", "p", "s%3Aabc.def")

    assert cached_login_session(URL, "u", "p") == "s%3Aabc.def"


def test_a_blank_session_id_is_not_remembered() -> None:
    remember_login_session(URL, "u", "p", "")

    assert cached_login_session(URL, "u", "p") is None


def test_clear_drops_every_session() -> None:
    remember_login_session(URL, "u", "p", "s%3Aabc.def")
    remember_login_session("http://uwazi.example.org", "u", "p", "s%3Axyz.uvw")

    clear_login_sessions()

    assert cached_login_session(URL, "u", "p") is None
    assert cached_login_session("http://uwazi.example.org", "u", "p") is None


def test_an_adapter_reuses_a_cached_session_without_logging_in() -> None:
    remember_login_session(URL, "u", "p", "s%3Aabc.def")

    adapter = HttpClientAdapter(url=URL, user="u", password="p")

    assert adapter.connect_sid == "s%3Aabc.def"
    header = get_cookie_header(
        adapter.request_adapter.cookies,
        Request("GET", f"{URL}/api/stats").prepare(),
    )
    assert header == "connect.sid=s%3Aabc.def"
