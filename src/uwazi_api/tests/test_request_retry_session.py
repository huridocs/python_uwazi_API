"""Isolated tests for the retry session policy and default timeout (no I/O)."""

from uwazi_api.adapters.request_retry import (
    DEFAULT_TIMEOUT,
    LOGIN_TIMEOUT,
    LOGIN_TRANSPORT_RETRIES,
    RETRY_TOTAL,
    TimeoutSession,
    requests_login_session,
    requests_retry_session,
    with_default_timeout,
    with_login_timeout,
)


def test_default_session_keeps_the_maintenance_retry_policy() -> None:
    session = requests_retry_session()
    try:
        assert isinstance(session, TimeoutSession)
        assert session.get_adapter("https://uwazi.example").max_retries.total == RETRY_TOTAL
    finally:
        session.close()


def test_default_timeout_is_injected_when_the_caller_sets_none() -> None:
    assert with_default_timeout({})["timeout"] == DEFAULT_TIMEOUT


def test_explicit_caller_timeout_is_preserved() -> None:
    assert with_default_timeout({"timeout": 1.5})["timeout"] == 1.5


# --- the login handshake -------------------------------------------------------
#
# Login runs before every other call, so a remote instance that was merely slow to
# answer it failed the whole run with a read timeout. The login session therefore
# carries its own, longer budget plus a couple of TRANSPORT retries — while a
# wrong URL or bad credentials must still fail fast, so it gets no status retries.


def test_login_session_retries_transport_but_never_a_status() -> None:
    session = requests_login_session()
    try:
        retries = session.get_adapter("https://uwazi.example").max_retries
        assert retries.connect == LOGIN_TRANSPORT_RETRIES
        assert retries.read == LOGIN_TRANSPORT_RETRIES
        # No status retries: the maintenance-window backoff must not be able to
        # turn a wrong scheme/URL into a multi-minute stall of the login thread.
        assert retries.status == 0
        assert not retries.status_forcelist
    finally:
        session.close()


def test_login_session_gives_login_its_own_longer_budget() -> None:
    """The shared API timeout's read budget was what killed slow remote instances."""
    assert LOGIN_TIMEOUT[1] > DEFAULT_TIMEOUT[1]
    assert with_login_timeout({})["timeout"] == LOGIN_TIMEOUT
    assert with_login_timeout({"timeout": 2.0})["timeout"] == 2.0


def test_the_login_session_still_times_out_every_request() -> None:
    session = requests_login_session()
    try:
        assert isinstance(session, TimeoutSession)
    finally:
        session.close()
