"""The logged-in session's Uwazi instance + credentials.

Every web page resolves the instance it works on from the session: the
operator picks the instance on the login screen, and the choice is bound to
the session there. Reading it from one place keeps the pages consistent and
keeps them free of any environment read (``UWAZI_URL`` may hold a LIST of
instances — the env is not the session's answer).

These helpers read ``app.storage.user``, so they may only be called from a
request context.
"""

from __future__ import annotations

from nicegui import app


def session_instance_url() -> str:
    """The logged-in session's instance URL, or '' when there is no session."""
    return app.storage.user.get("instance_url") or ""


def session_user() -> str:
    """The logged-in session's Uwazi user, or '' when there is no session."""
    return app.storage.user.get("user") or ""


def session_password() -> str:
    """The logged-in session's Uwazi password, or '' when there is no session."""
    return app.storage.user.get("password") or ""


__all__ = ["session_instance_url", "session_password", "session_user"]
