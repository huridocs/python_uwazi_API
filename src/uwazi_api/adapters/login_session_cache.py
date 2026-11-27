"""Process-local cache of Uwazi login cookies.

The web UI logs in once (password + optional 2FA code) and then builds a fresh
adapter for nearly every action. Re-running /api/login is impossible for a 2FA
account once its 30-second code expires, so the session cookie Uwazi issued at
login is reused, exactly as a browser would. The web login gate opts OUT of the
cache (``use_cached_session=False``) so every web login still proves the second
factor.
"""

from __future__ import annotations

import hashlib
import threading

_LOCK = threading.Lock()
# (normalized url, user, sha256(password)) -> connect.sid value.
_SESSIONS: dict[tuple[str, str, str], str] = {}


def _key(url: str, user: str, password: str) -> tuple[str, str, str]:
    digest = hashlib.sha256(password.encode("utf-8")).hexdigest()
    return (url.rstrip("/"), user, digest)


def remember_login_session(url: str, user: str, password: str, connect_sid: str) -> None:
    if not connect_sid:
        return
    with _LOCK:
        _SESSIONS[_key(url, user, password)] = connect_sid


def cached_login_session(url: str, user: str, password: str) -> str | None:
    with _LOCK:
        return _SESSIONS.get(_key(url, user, password))


def clear_login_sessions() -> None:
    with _LOCK:
        _SESSIONS.clear()
