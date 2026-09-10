"""HTTP retry policy for the Uwazi client.

Uwazi is fronted by a reverse proxy that serves a "We're down for maintenance"
page (HTTP 502/503/504) while the backend is briefly unavailable. Those are
transient: the request never reached the backend, so retrying it is safe and
idempotent. This module builds a ``requests.Session`` whose ``urllib3`` retry
policy absorbs a short maintenance window with exponential backoff instead of
failing the run.

The critical detail is ``allowed_methods``: urllib3's default allowlist omits
``POST``/``PATCH`` (non-idempotent), but Uwazi's write endpoints are ``POST``.
Without adding them, a 502 on an entity upload is returned immediately and the
agent fails/stalls. A maintenance 502 means the backend never saw the request,
so retrying ``POST`` is safe here.
"""

import logging
from types import TracebackType

import requests
from requests.adapters import HTTPAdapter
from urllib3.connectionpool import ConnectionPool
from urllib3.response import BaseHTTPResponse
from urllib3.util.retry import Retry

# Same channel as the existing "Error uploading entity ..." line in
# ``HttpClientAdapter``, so a maintenance retry shows up in the operator's logs.
logger = logging.getLogger("graylog")

# Transient server-side status codes that indicate Uwazi is temporarily
# unavailable (maintenance page, load balancer 502/503/504, rate limiting).
# Retried with exponential backoff so a short maintenance window is absorbed
# transparently instead of failing the run.
RETRY_STATUS_FORCELIST: tuple[int, ...] = (429, 500, 502, 503, 504)

# Total retries per request. With the backoff below this spans ~10 minutes,
# enough to ride out a "we'll be back in a few minutes" maintenance window.
RETRY_TOTAL: int = 10

# Base backoff (seconds). urllib3 sleeps ``backoff_factor * 2**(n-1)`` between
# attempts, capped at ``RETRY_BACKOFF_MAX``.
RETRY_BACKOFF_FACTOR: float = 2.0

# Cap on a single backoff sleep (seconds).
RETRY_BACKOFF_MAX: float = 120.0

# Jitter (seconds) added to each backoff sleep to desynchronize concurrent
# workers that would otherwise retry in lockstep.
RETRY_BACKOFF_JITTER: float = 0.5

# HTTP methods retried on a status code in ``RETRY_STATUS_FORCELIST``. The
# urllib3 default omits POST/PATCH; Uwazi's writes are POST, and a maintenance
# 502 means the backend never saw the request, so retrying is safe.
RETRY_ALLOWED_METHODS: frozenset[str] = frozenset({"GET", "PUT", "POST", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE"})

# Status codes that signal the backend is unreachable (maintenance / gateway).
_MAINTENANCE_STATUS_CODES: tuple[int, ...] = (502, 503, 504)

# Body-text markers that confirm a 5xx response is a maintenance page.
_MAINTENANCE_BODY_MARKERS: tuple[str, ...] = (
    "maintenance",
    "down for maintenance",
    "temporarily unavailable",
)


def is_maintenance_error(status_code: int | None, body: str | None = None) -> bool:
    """True when a response looks like Uwazi is down for maintenance.

    A maintenance page is served by the fronting proxy with a 502/503/504
    status; the status code alone is a strong signal (the backend is
    unreachable). For other 5xx codes, the body text is an extra confirmation.
    Pure: no I/O, no clocks.
    """
    if status_code in _MAINTENANCE_STATUS_CODES:
        return True
    if body and status_code is not None and 500 <= status_code < 600:
        lowered = body.lower()
        return any(marker in lowered for marker in _MAINTENANCE_BODY_MARKERS)
    return False


class _LoggingRetry(Retry):
    """A ``Retry`` that logs once when it first retries on a maintenance error.

    ``increment`` is called on every retry (status, connect, read, redirect), so
    the guard narrows to the FIRST status-code retry on a maintenance code: the
    ``history`` tuple is empty before the first retry and non-empty afterwards.
    """

    def increment(
        self,
        method: str | None = None,
        url: str | None = None,
        response: BaseHTTPResponse | None = None,
        error: Exception | None = None,
        _pool: ConnectionPool | None = None,
        _stacktrace: TracebackType | None = None,
    ) -> Retry:
        if (
            error is None
            and response is not None
            and not self.history
            and is_maintenance_error(getattr(response, "status", None))
        ):
            logger.warning(
                "maintenance detected (%s) on %s %s, retrying with backoff",
                getattr(response, "status", None),
                method,
                url,
            )
        return super().increment(
            method,
            url,
            response=response,
            error=error,
            _pool=_pool,
            _stacktrace=_stacktrace,
        )


def requests_retry_session(
    retries: int = RETRY_TOTAL,
    backoff_factor: float = RETRY_BACKOFF_FACTOR,
    status_forcelist: tuple[int, ...] = RETRY_STATUS_FORCELIST,
    session: requests.Session | None = None,
) -> requests.Session:
    """Build a ``requests.Session`` that retries transient errors with backoff."""
    session = session or requests.Session()
    retry = _LoggingRetry(
        total=retries,
        read=retries,
        connect=retries,
        backoff_factor=backoff_factor,
        backoff_max=RETRY_BACKOFF_MAX,
        backoff_jitter=RETRY_BACKOFF_JITTER,
        status_forcelist=status_forcelist,
        allowed_methods=RETRY_ALLOWED_METHODS,
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session
