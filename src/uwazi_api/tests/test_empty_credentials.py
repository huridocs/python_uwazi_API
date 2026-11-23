"""Isolated unit tests for the empty-credential refusal in the HTTP adapter.

No network: the adapter refuses an empty credential BEFORE it would log in, so
no request is made. The URL points at a closed port on purpose — a request
would fail loudly instead of hanging.
"""

from __future__ import annotations

import pytest

from uwazi_api.adapters.http_client_adapter import HttpClientAdapter
from uwazi_api.domain.exceptions import MissingCredentialsError

# A closed loopback port: a request would raise a connection error.
_DEAD_URL = "http://127.0.0.1:1"


def test_an_empty_username_raises_before_any_login() -> None:
    with pytest.raises(MissingCredentialsError):
        HttpClientAdapter(url=_DEAD_URL, user="", password="secret")


def test_an_empty_password_raises_before_any_login() -> None:
    with pytest.raises(MissingCredentialsError):
        HttpClientAdapter(url=_DEAD_URL, user="admin", password="")


def test_both_empty_raises_before_any_login() -> None:
    with pytest.raises(MissingCredentialsError):
        HttpClientAdapter(url=_DEAD_URL, user="", password="")


def test_a_blank_username_raises_before_any_login() -> None:
    with pytest.raises(MissingCredentialsError):
        HttpClientAdapter(url=_DEAD_URL, user="   ", password="secret")


def test_no_credentials_at_all_stays_the_public_client() -> None:
    # The documented public case: no login is attempted, so no credential is
    # required and no request is made.
    adapter = HttpClientAdapter(url=_DEAD_URL)

    assert adapter.user is None
    assert adapter.password is None
    assert adapter.connect_sid is None
