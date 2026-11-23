"""Bind the NiceGUI websocket to the browser session that owns the client.

NiceGUI dispatches a browser event by ``client_id`` ALONE: the socket.io
``event`` handler looks the client up in ``Client.instances`` and calls the
handler, with no check that the socket belongs to the session that created
that client. A socket that connects with no session cookie could therefore
drive another browser's client — opening dialogs, filling fields, submitting
them — and the server would execute it with that browser's Uwazi credentials.
A socket that carries its OWN valid session could also join another browser's
client room and receive the broadcasts meant for it.

The fix binds every socket to the browser session id its handshake carried
(:func:`session_id_from_environ`) and checks the client it claims at connect
time (:func:`client_id_from_environ`):

- a socket with NO browser session is refused at connect time;
- a socket whose claimed ``client_id`` belongs to another session is refused
  at connect time (this covers the browser's normal "implicit handshake",
  which happens INSIDE NiceGUI's connect handler);
- any later event whose target client belongs to another session is refused.

The decision helpers are pure; :func:`install` wires them into the socket.io
server. Mirrors ``host_guard.py``: the policy is testable without a running
server, and the wiring is a few lines.
"""

from __future__ import annotations

import urllib.parse
from typing import Any, Mapping

from loguru import logger

# socket.io session id -> the browser session id its handshake carried.
# ``None`` marks a socket that connected without a session (refused).
_SOCKET_SESSIONS: dict[str, str | None] = {}

# Guard against installing twice on the same server.
_INSTALLED: bool = False


def session_id_from_environ(environ: Mapping[str, Any] | None) -> str | None:
    """The browser session id carried by a socket.io handshake, or None.

    Starlette's ``SessionMiddleware`` processes the websocket scope too, so
    ``asgi.scope['session']`` holds the signed session the browser sent. The
    ``id`` is the one NiceGUI's storage middleware puts there and the one the
    session cookie carries. A socket without the cookie has an EMPTY session
    and therefore no id — that is exactly the case that must be refused.
    """
    if not environ:
        return None
    scope = environ.get("asgi.scope")
    if not isinstance(scope, Mapping):
        return None
    session = scope.get("session")
    if not isinstance(session, Mapping):
        return None
    value = session.get("id")
    return str(value) if value else None


def client_id_from_environ(environ: Mapping[str, Any] | None) -> str | None:
    """The ``client_id`` a socket claims in its handshake query, or None.

    NiceGUI puts its client id in the websocket URL (``?client_id=...``), so a
    socket announces which client it means to drive. Reading it here is what
    lets the guard refuse a socket that claims somebody else's client.
    """
    if not environ:
        return None
    query = environ.get("QUERY_STRING")
    if not query:
        return None
    values = urllib.parse.parse_qs(str(query)).get("client_id")
    if not values:
        return None
    return values[0] or None


def client_session_id(client: Any) -> str | None:
    """The browser session id that owns a NiceGUI client, or None.

    The client's ``request`` is the HTTP request that rendered its page, and
    its session is the one that request ran in. A client with no request (a
    synthetic one) has no owner, so it is nobody's to drive.
    """
    if client is None:
        return None
    try:
        request = client.request
    except Exception:  # noqa: BLE001 — NiceGUI raises when the request is unset
        return None
    session = getattr(request, "session", None)
    if not isinstance(session, Mapping):
        return None
    value = session.get("id")
    return str(value) if value else None


def socket_may_drive(socket_session_id: str | None, client_session_id: str | None) -> bool:
    """True when a socket may drive a client.

    Both ids must be present and equal. A socket with no session (no cookie)
    may drive nothing, and a socket may only drive clients of its OWN
    session — so a stolen or guessed client id is useless without the
    matching session cookie.
    """
    if not socket_session_id or not client_session_id:
        return False
    return socket_session_id == client_session_id


def install(sio: Any) -> None:
    """Bind the socket.io server's connections and events to the session.

    Wraps NiceGUI's own ``connect``, ``handshake``, ``event`` and
    ``disconnect`` handlers instead of replacing them, so NiceGUI's handshake
    and dispatch keep working. Idempotent: calling it twice is a no-op.
    """
    global _INSTALLED  # noqa: PLW0603 — one guard per process
    if _INSTALLED:
        return
    handlers = sio.handlers.setdefault("/", {})
    if "connect" not in handlers or "event" not in handlers:
        raise RuntimeError("the socket.io guard expects NiceGUI's handlers to be registered")

    original_connect = handlers["connect"]
    original_handshake = handlers.get("handshake")
    original_event = handlers["event"]
    original_disconnect = handlers.get("disconnect")

    async def guarded_connect(sid: str, *args: Any) -> Any:
        """Refuse a socket without a session, or one claiming another's client.

        socket.io calls the connect handler as ``(sid, environ, data)``; the
        environ is the second argument and carries both the session scope and
        the handshake query string.
        """
        from socketio.exceptions import ConnectionRefusedError

        environ = args[0] if args else None
        session_id = session_id_from_environ(environ)
        _SOCKET_SESSIONS[sid] = session_id
        if session_id is None:
            logger.warning("Refused a websocket connection without a browser session")
            raise ConnectionRefusedError("a browser session is required")

        claimed_client_id = client_id_from_environ(environ)
        if _client_is_foreign(claimed_client_id, session_id):
            logger.warning(
                "Refused a websocket connection claiming a client of another session (client_id={})",
                claimed_client_id,
            )
            raise ConnectionRefusedError("this session does not own the client")
        return await original_connect(sid, *args)

    async def guarded_handshake(sid: str, data: Any) -> Any:
        """Refuse to attach a socket to a client of another session."""
        socket_session = _SOCKET_SESSIONS.get(sid)
        client_id = data.get("client_id") if isinstance(data, dict) else None
        if not _client_is_drivable(client_id, socket_session):
            logger.warning(
                "Refused a websocket handshake for a client this session does not own (client_id={})",
                client_id,
            )
            return False
        return await original_handshake(sid, data)

    def guarded_event(sid: str, msg: Any) -> Any:
        """Refuse an event whose target client belongs to another session."""
        socket_session = _SOCKET_SESSIONS.get(sid)
        client_id = msg.get("client_id") if isinstance(msg, dict) else None
        if not _client_is_drivable(client_id, socket_session):
            logger.warning(
                "Refused a websocket event from a socket that does not own the target client (client_id={})",
                client_id,
            )
            return None
        return original_event(sid, msg)

    def guarded_disconnect(sid: str, *args: Any) -> Any:
        """Forget the socket's session when it disconnects."""
        _SOCKET_SESSIONS.pop(sid, None)
        if original_disconnect is None:
            return None
        # NiceGUI's disconnect handler takes the socket id only.
        try:
            return original_disconnect(sid)
        except TypeError:  # pragma: no cover — a future NiceGUI with more args
            return original_disconnect(sid, *args)

    handlers["connect"] = guarded_connect
    handlers["event"] = guarded_event
    handlers["disconnect"] = guarded_disconnect
    if original_handshake is not None:
        handlers["handshake"] = guarded_handshake
    _INSTALLED = True


def _client_is_foreign(client_id: Any, session_id: str | None) -> bool:
    """True when the named client exists but belongs to ANOTHER session.

    An unknown client is NOT foreign: a page rendered before a server restart
    names a client the server no longer has, and NiceGUI's own handshake
    refuses it (the browser then reloads the page). Refusing that at connect
    time would break the reconnect instead of letting NiceGUI handle it, so
    only a client that RESOLVES and belongs to somebody else is refused.
    """
    from nicegui.client import Client

    if not client_id or not session_id:
        return False
    client = Client.instances.get(str(client_id))
    if client is None:
        return False
    owner = client_session_id(client)
    if not owner:
        return False
    return owner != session_id


def _client_is_drivable(client_id: Any, session_id: str | None) -> bool:
    """True when the socket may drive the named client.

    Strict, unlike :func:`_client_is_foreign`: an event must name a client
    that EXISTS and belongs to this session. A socket that names an unknown
    client drives nothing.
    """
    from nicegui.client import Client

    if not client_id or not session_id:
        return False
    client = Client.instances.get(str(client_id))
    if client is None:
        return False
    return socket_may_drive(session_id, client_session_id(client))


def socket_session_for(sid: str) -> str | None:
    """The session id bound to a socket (None when it has none)."""
    return _SOCKET_SESSIONS.get(sid)


def reset() -> None:
    """Forget every socket binding (tests, and a fresh process)."""
    global _INSTALLED  # noqa: PLW0603 — one guard per process
    _SOCKET_SESSIONS.clear()
    _INSTALLED = False


__all__ = [
    "client_id_from_environ",
    "client_session_id",
    "install",
    "reset",
    "session_id_from_environ",
    "socket_may_drive",
    "socket_session_for",
]
