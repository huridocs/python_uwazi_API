"""Bind-address safety rules for the admin web app (pure, no NiceGUI import).

The admin agent holds a privileged Uwazi session, so an accidental ``0.0.0.0``
bind in cleartext is a production incident waiting to happen. ``main()`` binds
loopback by default and calls :func:`assert_safe_bind` before starting the
server; the checks live here so they can be tested without importing NiceGUI.

Pure: ``ipaddress`` only, no I/O.
"""

from __future__ import annotations

import ipaddress


def is_loopback_host(host: str) -> bool:
    """True for ``localhost`` / ``::1`` / any loopback IP literal."""
    if host in {"localhost", "::1"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def assert_safe_bind(host: str, public_url: str, storage_secret: str | None) -> None:
    """Refuse a non-loopback bind that is not behind HTTPS with a stable secret.

    TLS terminates at a reverse proxy, so the app cannot verify the certificate;
    ``ADMIN_WEB_PUBLIC_URL`` being ``https://`` is the operator's declaration
    that the proxy terminates TLS. Without a stable ``ADMIN_WEB_STORAGE_SECRET``
    a multi-process/restart deployment silently invalidates every session
    cookie, so both are required together.

    Raises ``SystemExit`` (the startup path) with an actionable message.
    """
    if is_loopback_host(host):
        return
    if not public_url.lower().startswith("https://"):
        raise SystemExit(
            f"Refusing to bind {host!r} in cleartext: set ADMIN_WEB_PUBLIC_URL to an "
            "https:// URL and terminate TLS at the reverse proxy, or bind 127.0.0.1."
        )
    if not storage_secret:
        raise SystemExit(
            "Refusing to bind a non-loopback interface without ADMIN_WEB_STORAGE_SECRET: "
            "session cookies would be signed with a per-restart secret."
        )


__all__ = ["assert_safe_bind", "is_loopback_host"]
