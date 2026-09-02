from abc import ABC, abstractmethod

from uwazi_admin_agent.domain.file_cache import FileCacheStats


class CacheStatsPort(ABC):
    """Aggregate cache counters for run-boundary observability.

    The mutating run steps (dry run / execute) reset the counters at their
    start and snapshot them at their end, so each boundary reports exactly one
    aggregate line ("24 fetched, 9,976 hits") — never one line per file. The
    snapshot is non-destructive: step drivers re-read it after the use case
    has already logged it, so both the log line and the printed report show
    the same window without double accounting.
    """

    @abstractmethod
    def reset_stats(self) -> None:
        """Zero all counters (called at the start of a run boundary)."""
        ...

    @abstractmethod
    def snapshot_stats(self) -> FileCacheStats:
        """Read the current counters without clearing them."""
        ...
