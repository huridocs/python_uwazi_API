"""The failed-login lockout of the admin web login gate.

A wrong password is counted per ACCOUNT (the instance + the username). After
``MAX_ATTEMPTS`` wrong passwords the account is BLOCKED: the login page then
refuses it without even contacting the Uwazi instance, until the service is
restarted (the state lives in memory only, so a restart clears it).

Why the account and not the IP: the gate is a single shared server, so an IP
counter would lock out every admin behind the same NAT or proxy, and a
per-account counter is what actually stops a password-spraying attempt on a
known username. The tradeoff is that a blocked account stays blocked for
everyone until the service restarts — that is the intended, explicit cost of
this simple design.

Only REFUSED credentials count (:class:`~uwazi_api.domain.exceptions.CredentialsRejectedError`):
an unreachable instance, a wrong URL or a 5xx is an operational failure and must
never lock an admin out.

Pure module: an in-memory dict, no I/O, no environment reads at import time.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from uwazi_admin_agent.domain.instance import instance_slug

#: Wrong passwords tolerated before the account is blocked.
MAX_ATTEMPTS: int = 4


def account_key(instance_url: str, user: str) -> str:
    """The key of one account: its instance plus its lower-cased username.

    The instance is part of the key so the same username on two tenants is
    tracked separately, and so a blocked account does not leak across
    instances. An empty instance yields a key without the instance part.
    """
    name = (user or "").strip().lower()
    slug = instance_slug(instance_url) if (instance_url or "").strip() else ""
    return f"{slug}|{name}" if slug else name


@dataclass
class _Account:
    """One account's failure count and block flag."""

    failures: int = 0
    blocked: bool = False


@dataclass
class LoginGuard:
    """In-memory failed-login accounting, one entry per account.

    Thread-safe: the NiceGUI server handles requests on a thread pool, so the
    counters are guarded by a lock.
    """

    max_attempts: int = MAX_ATTEMPTS
    _accounts: dict[str, _Account] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def is_blocked(self, instance_url: str, user: str) -> bool:
        """True when this account is blocked (no Uwazi call may be made)."""
        key = account_key(instance_url, user)
        with self._lock:
            account = self._accounts.get(key)
            return account is not None and account.blocked

    def record_failure(self, instance_url: str, user: str) -> int:
        """Count one refused login; return the account's failure count.

        The account becomes blocked once the count reaches ``max_attempts``.
        """
        key = account_key(instance_url, user)
        with self._lock:
            account = self._accounts.setdefault(key, _Account())
            account.failures += 1
            if account.failures >= self.max_attempts:
                account.blocked = True
            return account.failures

    def record_success(self, instance_url: str, user: str) -> None:
        """Clear the account's failures after a successful login."""
        key = account_key(instance_url, user)
        with self._lock:
            self._accounts.pop(key, None)

    def reset(self) -> None:
        """Unblock every account (a service restart does this implicitly)."""
        with self._lock:
            self._accounts.clear()


#: The one guard the web app uses. Process-local, like the credential vault.
GUARD: LoginGuard = LoginGuard()


def blocked_message(user: str) -> str:
    """The message shown to a blocked account."""
    return f"Too many failed login attempts for {user!r}. This account is locked until the admin agent service is restarted."


__all__ = [
    "GUARD",
    "MAX_ATTEMPTS",
    "LoginGuard",
    "account_key",
    "blocked_message",
]
