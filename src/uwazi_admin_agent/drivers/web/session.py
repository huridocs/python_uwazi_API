"""The logged-in session's Uwazi instance + credentials.

Every web page resolves the instance it works on from the session: the
operator picks the instance on the login screen, and the choice is bound to
the session there. Reading it from one place keeps the pages consistent and
keeps them free of any environment read (``UWAZI_URL`` may hold a LIST of
instances — the env is not the session's answer).

The PASSWORD is never written to disk. NiceGUI's ``app.storage.user`` is a
persistent, server-side JSON document, so storing the password there left it
in plaintext on disk. Instead, the session persists only a random, non-secret
vault token; the credentials live in :data:`_VAULT` in this process's memory,
keyed by that token, and are dropped on logout. A restarted server therefore
logs everyone out (an acceptable, safe outcome) instead of reading passwords
back from disk.

These helpers read ``app.storage.user``, so they may only be called from a
request context (a page build, an event handler, or a client timer — all of
which carry NiceGUI's request contextvar). The page-bound client helpers below
cache the instance on the NiceGUI client so background broadcasts can address
it without a request.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

from nicegui import app, context
from nicegui.slot import Slot

from uwazi_admin_agent.domain.instance import same_instance

# Token key persisted in ``app.storage.user``: a random identifier, never a secret.
_TOKEN_KEY = "credential_token"
# Non-secret session fields, kept for display/gating only.
_USER_KEY = "user"
_INSTANCE_KEY = "instance_url"

# Absolute lifetime of a vault entry, in seconds. The password lives in process
# memory only; this bounds how long an abandoned session keeps it there. The
# default is 12 hours; tighten it for a stricter deployment.
_SESSION_TTL_SECONDS: float = float(os.environ.get("ADMIN_WEB_SESSION_TTL_SECONDS", "43200"))
# Upper bound on concurrently retained sessions (abandoned tabs must not grow
# the vault without limit). The oldest entry is evicted when the cap is hit.
_SESSION_MAX_ENTRIES: int = int(os.environ.get("ADMIN_WEB_SESSION_MAX_ENTRIES", "256"))


@dataclass(frozen=True)
class SessionCredentials:
    """One logged-in session's Uwazi target and account (kept in memory only)."""

    instance_url: str
    user: str
    password: str


@dataclass(frozen=True)
class _VaultEntry:
    """One in-memory credential entry plus its absolute expiry."""

    credentials: SessionCredentials
    expires_at: float


def _clock() -> float:
    """Monotonic seconds; injectable so the expiry logic is testable."""
    return time.monotonic()


_LOCK = threading.Lock()
# token -> _VaultEntry. Process memory only: never serialized, never on disk.
_VAULT: dict[str, _VaultEntry] = {}


def is_expired(entry: _VaultEntry, now: float) -> bool:
    """True when ``entry`` has reached its absolute deadline (pure)."""
    return now >= entry.expires_at


def store_entry(
    vault: dict[str, _VaultEntry],
    token: str,
    credentials: SessionCredentials,
    *,
    now: float,
    ttl_seconds: float = _SESSION_TTL_SECONDS,
    max_entries: int = _SESSION_MAX_ENTRIES,
) -> None:
    """Insert one entry, dropping expired ones and evicting the oldest (pure).

    Dict insertion order is the eviction order: ``remember_credentials`` always
    appends, so the first key is the least recently created session.
    """
    for expired_token in [key for key, entry in vault.items() if is_expired(entry, now)]:
        del vault[expired_token]
    while max_entries > 0 and len(vault) >= max_entries:
        del vault[next(iter(vault))]
    vault[token] = _VaultEntry(credentials=credentials, expires_at=now + ttl_seconds)


def load_entry(vault: dict[str, _VaultEntry], token: str, *, now: float) -> _VaultEntry | None:
    """Return a live entry, deleting it when expired (pure)."""
    if not token:
        return None
    entry = vault.get(token)
    if entry is None:
        return None
    if is_expired(entry, now):
        del vault[token]
        return None
    return entry


def _storage() -> Any:
    """The session's storage dict, or None outside a request context (e.g. a worker thread)."""
    try:
        return app.storage.user
    except RuntimeError:
        return None


def _token() -> str:
    storage = _storage()
    if storage is None:
        return ""
    return str(storage.get(_TOKEN_KEY) or "")


def remember_credentials(instance_url: str, user: str, password: str) -> None:
    """Bind validated credentials to THIS session (memory only).

    The persisted document keeps only the non-secret user/instance (for the
    header and the login gate) and a random token that names the vault entry.
    Any legacy plaintext ``password`` key from an older build is dropped.
    """
    token = uuid.uuid4().hex
    credentials = SessionCredentials(instance_url=instance_url, user=user, password=password)
    with _LOCK:
        store_entry(_VAULT, token, credentials, now=_clock())
    storage = app.storage.user
    storage[_TOKEN_KEY] = token
    storage[_USER_KEY] = user
    storage[_INSTANCE_KEY] = instance_url
    storage.pop("password", None)


def forget_credentials() -> None:
    """Drop this session's credentials from memory and from the persisted document."""
    storage = _storage()
    if storage is None:
        return
    token = str(storage.pop(_TOKEN_KEY, None) or "")
    if token:
        with _LOCK:
            _VAULT.pop(token, None)
    # Legacy builds persisted the password here; remove it on the way out too.
    storage.pop("password", None)
    storage.pop(_USER_KEY, None)
    storage.pop(_INSTANCE_KEY, None)


def _credentials_for_token(token: str) -> SessionCredentials | None:
    with _LOCK:
        entry = load_entry(_VAULT, token, now=_clock())
    return entry.credentials if entry is not None else None


def session_credentials() -> SessionCredentials | None:
    """The logged-in session's credentials, or None when there is no session."""
    return _credentials_for_token(_token())


def client_credentials(client: Any) -> SessionCredentials | None:
    """The credentials this CLIENT was built with, or None after its logout.

    Background broadcasts run outside a request context, so they cannot read
    ``app.storage.user``; the token cached on the client at page build names
    the same vault entry, and disappears the moment that session logs out.
    """
    return _credentials_for_token(getattr(client, "_session_token", "") or "")


def client_is_active(client: Any, instance: str) -> bool:
    """True when ``client``'s session is still logged in AND on ``instance``."""
    credentials = client_credentials(client)
    return credentials is not None and same_instance(credentials.instance_url, instance)


def session_instance_url() -> str:
    """The logged-in session's instance URL, or '' when there is no session."""
    credentials = session_credentials()
    return credentials.instance_url if credentials is not None else ""


def session_user() -> str:
    """The logged-in session's Uwazi username, or '' when there is no session."""
    credentials = session_credentials()
    return credentials.user if credentials is not None else ""


def _current_client() -> Any:
    """The client this code runs for, or None outside any page/timer context.

    ``context.client`` would CREATE a placeholder client when the task has no
    slot (script mode, tests), so the slot stack is inspected first: a check
    that must not have side effects cannot use the convenience property.
    """
    stack = Slot.get_stack()
    return stack[-1].parent.client if stack else None


def session_is_active(instance: str) -> bool:
    """True when the session is logged in AND bound to ``instance``.

    Used by the per-page timers: a page whose session logged out (in this tab
    or another one) or switched instance must not keep reading that instance's
    runs, tasks or executions. A bound client is authoritative for its own
    page; outside a page context the request's session decides.
    """
    client = _current_client()
    if client is not None and getattr(client, "_session_token", ""):
        return client_is_active(client, instance)
    credentials = session_credentials()
    return credentials is not None and same_instance(credentials.instance_url, instance)


def credentials_for_instance(instance: str) -> SessionCredentials | None:
    """The session's credentials when they target ``instance``, else None.

    A page captured ``instance`` when it was built; the session may since have
    logged out or switched instance (the two tabs of one browser share the
    session). Acting on the page's old instance with a new session's account —
    or with none at all — is exactly what this refuses.
    """
    credentials = session_credentials()
    if credentials is None or not same_instance(credentials.instance_url, instance):
        return None
    return credentials


def bind_client_instance() -> None:
    """Cache this page's instance on its NiceGUI client (background-safe).

    Broadcasts run outside any request context, so they cannot read
    ``app.storage.user``. Every page that may receive them calls this once at
    build time; the bound value is also what lets an unregistered (login-page)
    client be skipped entirely.
    """
    context.client._session_instance_url = session_instance_url()  # noqa: SLF001 — per-client binding
    context.client._session_token = _token()  # noqa: SLF001 — the vault entry behind it


def clear_client_instance() -> None:
    """Unbind the client (logout/login page): a stale binding must get no toasts."""
    context.client._session_instance_url = ""  # noqa: SLF001 — per-client binding
    context.client._session_token = ""  # noqa: SLF001 — a logged-out client holds no credentials


def client_instance_url(client: Any) -> str:
    """The instance bound to a client at page build, or '' when unregistered."""
    return getattr(client, "_session_instance_url", "") or ""


def page_instance_url() -> str:
    """The instance THIS page was built for, or '' when there is none to serve.

    A page captures its instance at build time (the client binding); reading
    the session live would follow a later login/logout in another tab and mix
    two instances' data on one page. The bound instance is returned only while
    this session is still logged in to it: after a logout (or a switch to
    another instance), every data path gets '' — and every store builder
    refuses '' — instead of serving the previous instance's runs.
    """
    client = _current_client()
    if client is None:
        return session_instance_url()
    bound = client_instance_url(client)
    return bound if bound and session_is_active(bound) else ""


__all__ = [
    "SessionCredentials",
    "bind_client_instance",
    "clear_client_instance",
    "client_credentials",
    "client_instance_url",
    "client_is_active",
    "credentials_for_instance",
    "forget_credentials",
    "is_expired",
    "load_entry",
    "page_instance_url",
    "remember_credentials",
    "session_credentials",
    "session_instance_url",
    "session_is_active",
    "store_entry",
    "session_user",
]
