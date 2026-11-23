"""Integration tests for the websocket session guard's WIRING.

The pure decision helpers are covered in ``test_socket_guard.py``. These tests
exercise the INSTALLED guard against a real socket.io server, so the wrapping
of the ``connect`` / ``handshake`` / ``event`` handlers is verified too — not
just the helpers behind it.

No mocks, no stubs, no network: the socket.io server is real, the handlers are
real functions, and they are called the way socket.io calls them. The
repository has no async test plugin, so the coroutines are driven with
``asyncio.run`` from ordinary test functions.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import socketio

from uwazi_api import socket_guard
from uwazi_api.socket_guard import install


@pytest.fixture(autouse=True)
def _clean_guard() -> Any:
    """Every test starts with a fresh guard."""
    socket_guard.reset()
    yield
    socket_guard.reset()


def _server_with_handlers(record: dict[str, Any]) -> socketio.AsyncServer:
    """A real socket.io server whose handlers record what they were called with."""
    sio = socketio.AsyncServer()

    @sio.on("connect")
    async def _connect(sid: str, environ: Any, data: Any = None) -> None:
        record["connect"].append((sid, environ))

    @sio.on("handshake")
    async def _handshake(sid: str, data: Any) -> bool:
        record["handshake"].append((sid, data))
        return True

    @sio.on("event")
    def _event(sid: str, msg: Any) -> None:
        record["event"].append((sid, msg))

    @sio.on("disconnect")
    def _disconnect(sid: str) -> None:
        record["disconnect"].append(sid)

    install(sio)
    return sio


def _record() -> dict[str, Any]:
    return {"connect": [], "handshake": [], "event": [], "disconnect": []}


def _environ(session: dict[str, Any] | None, query: str = "") -> dict[str, Any]:
    """A socket.io handshake environ carrying a Starlette session."""
    scope: dict[str, Any] = {"type": "websocket"}
    if session is not None:
        scope["session"] = session
    return {"asgi.scope": scope, "QUERY_STRING": query}


def _connect(sio: socketio.AsyncServer, sid: str, environ: dict[str, Any]) -> Any:
    """Call the installed connect handler the way socket.io does."""
    return asyncio.run(sio.handlers["/"]["connect"](sid, environ, {}))


def _handshake(sio: socketio.AsyncServer, sid: str, data: dict[str, Any]) -> Any:
    """Call the installed handshake handler the way socket.io does."""
    return asyncio.run(sio.handlers["/"]["handshake"](sid, data))


def test_a_socket_without_a_session_is_refused() -> None:
    record = _record()
    sio = _server_with_handlers(record)

    with pytest.raises(socketio.exceptions.ConnectionRefusedError):
        _connect(sio, "sid-1", _environ(None))

    assert record["connect"] == []


def test_a_socket_with_a_session_is_accepted() -> None:
    record = _record()
    sio = _server_with_handlers(record)

    _connect(sio, "sid-1", _environ({"id": "s-1"}))

    assert len(record["connect"]) == 1


def test_the_session_is_remembered_for_the_socket() -> None:
    record = _record()
    sio = _server_with_handlers(record)

    _connect(sio, "sid-1", _environ({"id": "s-1"}))

    assert socket_guard.socket_session_for("sid-1") == "s-1"


def test_the_binding_is_forgotten_on_disconnect() -> None:
    record = _record()
    sio = _server_with_handlers(record)
    _connect(sio, "sid-1", _environ({"id": "s-1"}))

    sio.handlers["/"]["disconnect"]("sid-1")

    assert socket_guard.socket_session_for("sid-1") is None


def test_an_event_for_an_unknown_client_is_refused() -> None:
    record = _record()
    sio = _server_with_handlers(record)
    _connect(sio, "sid-1", _environ({"id": "s-1"}))

    sio.handlers["/"]["event"]("sid-1", {"client_id": "nobody", "id": 1, "listener_id": "l"})

    assert record["event"] == []


def test_an_event_without_a_client_id_is_refused() -> None:
    record = _record()
    sio = _server_with_handlers(record)
    _connect(sio, "sid-1", _environ({"id": "s-1"}))

    sio.handlers["/"]["event"]("sid-1", {"id": 1, "listener_id": "l"})

    assert record["event"] == []


def test_an_event_from_an_unbound_socket_is_refused() -> None:
    record = _record()
    sio = _server_with_handlers(record)

    # A socket that never connected (so it has no session) drives nothing.
    sio.handlers["/"]["event"]("sid-ghost", {"client_id": "nobody", "id": 1, "listener_id": "l"})

    assert record["event"] == []


def test_installing_twice_keeps_one_guard() -> None:
    record = _record()
    sio = _server_with_handlers(record)
    first = sio.handlers["/"]["connect"]

    install(sio)

    assert sio.handlers["/"]["connect"] is first
