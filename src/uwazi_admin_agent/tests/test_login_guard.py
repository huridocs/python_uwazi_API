"""Isolated unit tests for the admin web login guard (pure, offline).

No I/O, no environment reads, no mocks: the guard is a pure in-memory
accounting over literal strings.
"""

from __future__ import annotations

from uwazi_admin_agent.domain.login_guard import MAX_ATTEMPTS, LoginGuard, account_key, blocked_message


def test_account_key_lowercases_the_username() -> None:
    assert account_key("http://localhost:3000", "Admin") == account_key("http://localhost:3000", "admin")


def test_account_key_separates_the_instances() -> None:
    assert account_key("http://a", "admin") != account_key("http://b", "admin")


def test_account_key_without_an_instance_is_the_username() -> None:
    assert account_key("", "admin") == "admin"


def test_a_fresh_account_is_not_blocked() -> None:
    guard = LoginGuard()

    assert guard.is_blocked("http://a", "admin") is False


def test_the_account_is_blocked_after_the_maximum_failures() -> None:
    guard = LoginGuard()

    for expected in range(1, MAX_ATTEMPTS):
        assert guard.record_failure("http://a", "admin") == expected
        assert guard.is_blocked("http://a", "admin") is False

    assert guard.record_failure("http://a", "admin") == MAX_ATTEMPTS
    assert guard.is_blocked("http://a", "admin") is True


def test_a_blocked_account_stays_blocked_without_new_failures() -> None:
    guard = LoginGuard()
    for _ in range(MAX_ATTEMPTS):
        guard.record_failure("http://a", "admin")

    assert guard.is_blocked("http://a", "admin") is True


def test_a_success_clears_the_failure_count() -> None:
    guard = LoginGuard()
    guard.record_failure("http://a", "admin")
    guard.record_failure("http://a", "admin")

    guard.record_success("http://a", "admin")

    assert guard.is_blocked("http://a", "admin") is False
    # The count starts over: MAX_ATTEMPTS more failures are still needed.
    for _ in range(MAX_ATTEMPTS - 1):
        guard.record_failure("http://a", "admin")
    assert guard.is_blocked("http://a", "admin") is False


def test_a_failure_on_one_instance_does_not_block_another() -> None:
    guard = LoginGuard()
    for _ in range(MAX_ATTEMPTS):
        guard.record_failure("http://a", "admin")

    assert guard.is_blocked("http://b", "admin") is False


def test_a_failure_on_one_user_does_not_block_another() -> None:
    guard = LoginGuard()
    for _ in range(MAX_ATTEMPTS):
        guard.record_failure("http://a", "admin")

    assert guard.is_blocked("http://a", "operator") is False


def test_reset_unblocks_every_account() -> None:
    guard = LoginGuard()
    for _ in range(MAX_ATTEMPTS):
        guard.record_failure("http://a", "admin")

    guard.reset()

    assert guard.is_blocked("http://a", "admin") is False


def test_the_blocked_message_names_the_account_and_the_way_out() -> None:
    message = blocked_message("admin")

    assert "admin" in message
    assert "restart" in message.lower()


def test_the_default_maximum_is_four_attempts() -> None:
    assert MAX_ATTEMPTS == 4
