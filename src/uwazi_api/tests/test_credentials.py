"""Isolated unit tests for the credential rule (pure, offline).

No I/O, no environment reads, no mocks: the rule is pure functions over
literal strings.
"""

from __future__ import annotations

import pytest

from uwazi_api.domain.credentials import (
    MISSING_CREDENTIALS_MESSAGE,
    MissingCredentialsError,
    has_credentials,
    require_credentials,
)


def test_both_credentials_present_is_accepted() -> None:
    assert has_credentials("admin", "secret") is True


def test_an_empty_username_is_refused() -> None:
    assert has_credentials("", "secret") is False


def test_an_empty_password_is_refused() -> None:
    assert has_credentials("admin", "") is False


def test_a_none_credential_is_refused() -> None:
    assert has_credentials(None, "secret") is False
    assert has_credentials("admin", None) is False


def test_a_blank_username_is_refused() -> None:
    # A stray space is not a username.
    assert has_credentials("   ", "secret") is False


def test_a_blank_password_is_refused() -> None:
    assert has_credentials("admin", "   ") is False


def test_require_credentials_returns_the_values_unchanged() -> None:
    # The password may legitimately contain spaces, so it is never stripped.
    assert require_credentials("admin", " secret ") == ("admin", " secret ")


def test_require_credentials_refuses_an_empty_username() -> None:
    with pytest.raises(MissingCredentialsError):
        require_credentials("", "secret")


def test_require_credentials_refuses_an_empty_password() -> None:
    with pytest.raises(MissingCredentialsError):
        require_credentials("admin", "")


def test_the_message_names_both_credentials_and_no_default() -> None:
    assert "username" in MISSING_CREDENTIALS_MESSAGE.lower()
    assert "password" in MISSING_CREDENTIALS_MESSAGE.lower()
    assert "no default" in MISSING_CREDENTIALS_MESSAGE.lower()


def test_the_missing_credentials_error_is_an_authentication_error() -> None:
    from uwazi_api.domain.exceptions import AuthenticationError

    assert issubclass(MissingCredentialsError, AuthenticationError)
