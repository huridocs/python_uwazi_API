"""Isolated unit tests for the websocket session guard (pure, offline).

No I/O, no network, no mocks of the framework: the guard's decision helpers
are pure functions over literal mappings.
"""

from __future__ import annotations

from typing import Any

from uwazi_api.socket_guard import (
    client_id_from_environ,
    client_session_id,
    session_id_from_environ,
    socket_may_drive,
)


def _environ(session: dict[str, Any] | None, query: str = "") -> dict[str, Any]:
    """A socket.io handshake environ carrying a Starlette session."""
    scope: dict[str, Any] = {"type": "websocket"}
    if session is not None:
        scope["session"] = session
    environ: dict[str, Any] = {"asgi.scope": scope, "HTTP_COOKIE": "session=abc"}
    if query:
        environ["QUERY_STRING"] = query
    return environ


class _Client:
    """A stand-in for a NiceGUI client (only the session is read)."""

    def __init__(self, session: dict[str, Any] | None) -> None:
        self.request = type("Request", (), {"session": session or {}})()


# --- the session id carried by a handshake -----------------------------------


def test_the_session_id_is_read_from_the_handshake_scope() -> None:
    assert session_id_from_environ(_environ({"id": "s-1"})) == "s-1"


def test_a_handshake_without_a_session_has_no_id() -> None:
    assert session_id_from_environ(_environ(None)) is None


def test_a_handshake_with_an_empty_session_has_no_id() -> None:
    assert session_id_from_environ(_environ({})) is None


def test_a_handshake_without_an_id_has_no_id() -> None:
    assert session_id_from_environ(_environ({"other": "value"})) is None


def test_no_environ_has_no_id() -> None:
    assert session_id_from_environ(None) is None
    assert session_id_from_environ({}) is None


def test_an_environ_without_a_scope_has_no_id() -> None:
    assert session_id_from_environ({"HTTP_COOKIE": "session=abc"}) is None


# --- the client id a socket claims -------------------------------------------


def test_the_claimed_client_id_is_read_from_the_handshake_query() -> None:
    assert client_id_from_environ(_environ({"id": "s-1"}, "client_id=c-1&transport=websocket")) == "c-1"


def test_a_handshake_without_a_client_id_claims_none() -> None:
    assert client_id_from_environ(_environ({"id": "s-1"}, "transport=websocket")) is None


def test_a_handshake_without_a_query_claims_none() -> None:
    assert client_id_from_environ(_environ({"id": "s-1"})) is None


def test_no_environ_claims_no_client() -> None:
    assert client_id_from_environ(None) is None


# --- the session id that owns a client ---------------------------------------


def test_the_client_session_id_is_read_from_its_request() -> None:
    assert client_session_id(_Client({"id": "s-1"})) == "s-1"


def test_a_client_without_a_session_has_no_id() -> None:
    assert client_session_id(_Client(None)) is None
    assert client_session_id(None) is None


# --- the decision ------------------------------------------------------------


def test_a_socket_may_drive_a_client_of_its_own_session() -> None:
    assert socket_may_drive("s-1", "s-1") is True


def test_a_socket_may_not_drive_a_client_of_another_session() -> None:
    assert socket_may_drive("s-1", "s-2") is False


def test_a_socket_without_a_session_may_drive_nothing() -> None:
    assert socket_may_drive(None, "s-1") is False


def test_a_socket_may_not_drive_a_client_without_a_session() -> None:
    assert socket_may_drive("s-1", None) is False


def test_two_sockets_without_sessions_may_not_drive_each_other() -> None:
    assert socket_may_drive(None, None) is False


def test_an_empty_session_id_is_refused() -> None:
    assert socket_may_drive("", "s-1") is False
    assert socket_may_drive("s-1", "") is False
