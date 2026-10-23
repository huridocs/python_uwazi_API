"""Local-instance guard for live end-to-end (E2E) tests.

The E2E suite reads and writes REAL data on a running Uwazi instance. To make an
accidental production run impossible, every E2E test must call
:func:`require_local_e2e_target` before it builds a client. Only loopback
targets are accepted; any other host is refused with a clear error message.

Resolution order for the target is the explicit argument, then ``UWAZI_URL``,
then ``http://localhost:3000`` (the same fallback the E2E modules use).
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

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


def is_local_e2e_target(url: str | None) -> bool:
    """True only when ``url`` points at ``localhost``, ``127.0.0.1`` or ``::1``.

    ``None`` means "the default target" (localhost). An explicitly empty string
    is not local: it is a configuration error and the caller should refuse it.
    """
    target = DEFAULT_UWAZI_URL if url is None else url
    return _host_of(target) in LOOPBACK_HOSTS


def require_local_e2e_target(url: str | None = None) -> str:
    """Return the resolved target URL, refusing anything that is not loopback.

    Call this as the FIRST statement of an E2E test class's ``setup_class`` (or
    of the test itself when it is not class-based), before any ``UwaziClient`` /
    ``UwaziApiAdapter`` is constructed. A non-local target raises
    :class:`LocalE2ETargetError` with an operator-facing explanation.
    """
    target = url if url is not None else os.environ.get("UWAZI_URL")
    if target is None:
        target = DEFAULT_UWAZI_URL
    target = target.strip()
    host = _host_of(target)
    if host in LOOPBACK_HOSTS:
        return target

    raise LocalE2ETargetError(
        "E2E tests refused: the configured target is not a localhost instance.\n"
        f"\n"
        f"  UWAZI_URL = {target}\n"
        f"  host      = {host or '(unparsable)'}\n"
        f"\n"
        f"Live end-to-end tests create and modify real data, so they may only run\n"
        f"against a local Uwazi instance (localhost, 127.0.0.1 or ::1). Point\n"
        f"UWAZI_URL at the local instance and try again, for example:\n"
        f"\n"
        f"  UWAZI_URL=http://localhost:3000 just test_e2e\n"
        f"\n"
        f"Nothing was sent to {target}."
    )
