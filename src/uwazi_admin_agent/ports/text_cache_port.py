from abc import ABC, abstractmethod


class TextCachePort(ABC):
    """Persistent text cache for expensive LLM and translation results.

    The cache is a content-addressed store: callers build a deterministic key
    from the request's inputs (see :mod:`uwazi_admin_agent.domain.text_cache`)
    and the implementation returns the exact text previously stored for it.
    Entries never expire on their own — a changed request is a different key —
    and the instance namespace / eviction policy live in the implementation.
    """

    @abstractmethod
    def get_text(self, key: str) -> str | None:
        """Return the cached text for ``key``, or None on a miss."""
        ...

    @abstractmethod
    def put_text(self, key: str, value: str) -> None:
        """Store ``value`` under ``key`` (atomic, best-effort)."""
        ...
