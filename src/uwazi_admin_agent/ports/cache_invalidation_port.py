from abc import ABC, abstractmethod
from collections.abc import Sequence


class CacheInvalidationPort(ABC):
    """Explicit write-path invalidation for cached entity raws.

    Raw entries are mutable state mirrored from Uwazi, so every write our own
    code performs must drop the affected entities' cached rows immediately —
    the TTL alone would leave a window in which our own writes are invisible.

    Implementations remove every cached language row of each shared_id: row
    fields are per-locale, but the denormalized documents/attachments and the
    bidirectional relations in a fetched raw are shared across the
    shared_id's rows, so invalidating one language would leave the others
    stale.
    """

    @abstractmethod
    def invalidate_entities(self, shared_ids: Sequence[str]) -> None:
        """Drop all cached raws for ``shared_ids`` (no-op for unknown ids)."""
        ...
