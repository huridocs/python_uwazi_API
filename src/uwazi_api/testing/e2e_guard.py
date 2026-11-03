"""Local-instance guard for live end-to-end (E2E) tests.

The E2E suite reads and writes REAL data on a running Uwazi instance. To make an
accidental production run impossible, every E2E test must call
:func:`require_local_e2e_target` before it builds a client. Only loopback
targets are accepted; any other host is refused with a clear error message.

Resolution order for the target is the explicit argument, then ``UWAZI_URL``,
then ``http://localhost:3000`` (the same fallback the E2E modules use).

``UWAZI_URL`` may hold a comma-separated LIST of instances (the admin agent
accepts one). E2E tests write, so EVERY configured instance must be loopback:
a list is refused when any of its entries is not local, even though the tests
themselves would only use the first one. The returned target is always that
single first URL.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

from uwazi_api.instance_urls import parse_instance_urls

DEFAULT_UWAZI_URL: str = "http://localhost:3000"

# Only the loopback interface is safe for live writes. Do not add hostnames
# here: a shared staging server is still a real instance with real data.
LOOPBACK_HOSTS: frozenset[str] = frozenset({"localhost", "127.0.0.1", "::1"})


class LocalE2ETargetError(RuntimeError):
    """Raised when an E2E test is pointed at a non-local Uwazi instance."""


def _host_of(url: str) -> str:
    """Return the lower-cased host of ``url`` (tolerates a missing scheme)."""
    candidate = url.strip()
    if "://" not in candidate:
        candidate = f"http://{candidate}"
    return (urlparse(candidate).hostname or "").lower()


def _configured_urls(raw: str | None) -> list[str]:
    """Every URL a configured value names (a single URL is a one-item list)."""
    return parse_instance_urls(raw) if raw is not None else [DEFAULT_UWAZI_URL]


def is_local_e2e_target(url: str | None) -> bool:
    """True only when EVERY URL in ``url`` points at loopback.

    ``None`` means "the default target" (localhost). An explicitly empty string
    is not local: it is a configuration error and the caller should refuse it.
    A comma-separated list is local only when all of its entries are.
    """
    targets = _configured_urls(url)
    return bool(targets) and all(_host_of(target) in LOOPBACK_HOSTS for target in targets)


def require_local_e2e_target(url: str | None = None) -> str:
    """Return the single target URL, refusing anything that is not loopback.

    Call this as the FIRST statement of an E2E test class's ``setup_class`` (or
    of the test itself when it is not class-based), before any ``UwaziClient`` /
    ``UwaziApiAdapter`` is constructed. A non-local target raises
    :class:`LocalE2ETargetError` with an operator-facing explanation.

    A configured LIST is resolved to its first URL (the repo-wide
    single-instance convention), so the value returned here can be used
    directly as the client's URL.
    """
    targets = _configured_urls(url if url is not None else os.environ.get("UWAZI_URL"))
    offending = [target for target in targets if _host_of(target) not in LOOPBACK_HOSTS]
    if not offending:
        return targets[0]

    shown = ", ".join(offending)
    raise LocalE2ETargetError(
        "E2E tests refused: the configured target is not a localhost instance.\n"
        f"\n"
        f"  UWAZI_URL = {shown}\n"
        f"  host      = {', '.join(_host_of(target) or '(unparsable)' for target in offending)}\n"
        f"\n"
        f"Live end-to-end tests create and modify real data, so they may only run\n"
        f"against a local Uwazi instance (localhost, 127.0.0.1 or ::1). Point\n"
        f"UWAZI_URL at the local instance and try again, for example:\n"
        f"\n"
        f"  UWAZI_URL=http://localhost:3000 just test_e2e\n"
        f"\n"
        f"Nothing was sent to {shown}."
    )
