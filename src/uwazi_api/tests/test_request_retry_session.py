"""Isolated tests for the retry session policy and default timeout (no I/O)."""

from uwazi_api.adapters.request_retry import (
    DEFAULT_TIMEOUT,
    RETRY_TOTAL,
    TimeoutSession,
    requests_retry_session,
    with_default_timeout,
)


def test_default_session_keeps_the_maintenance_retry_policy() -> None:
    session = requests_retry_session()
    try:
        assert isinstance(session, TimeoutSession)
        assert session.get_adapter("https://uwazi.example").max_retries.total == RETRY_TOTAL
    finally:
        session.close()


def test_zero_retries_disables_the_backoff_for_login() -> None:
    # Login must fail in seconds; this session backs HttpClientAdapter's
    # _get_connect_sid, where the ~10-minute backoff used to freeze callers.
    session = requests_retry_session(retries=0)
    try:
        retries = session.get_adapter("https://uwazi.example").max_retries
        assert retries.total == 0
        assert retries.connect == 0
        assert retries.read == 0
    finally:
        session.close()


def test_default_timeout_is_injected_when_the_caller_sets_none() -> None:
    assert with_default_timeout({})["timeout"] == DEFAULT_TIMEOUT


def test_explicit_caller_timeout_is_preserved() -> None:
    assert with_default_timeout({"timeout": 1.5})["timeout"] == 1.5
