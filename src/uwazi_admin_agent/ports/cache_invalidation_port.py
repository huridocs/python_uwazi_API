from abc import ABC, abstractmethod
from collections.abc import Sequence


class CacheInvalidationPort(ABC):
    """Explicit write-path invalidation for the cached entity raws + file bytes.

    Design principle: cache eviction is LOSSLESS by construction. The cache is
    a read-through mirror — the sources of truth are Uwazi itself and the run's
    backup store — so dropping an entry can only cost a re-fetch, never data.
    When in doubt, invalidate: entries repopulate lazily on the next read (the
    "re-cache" behavior), so correctness never trades against durability.

    Raw entries are mutable state mirrored from Uwazi, so every write our own
    code performs must drop the affected entities' cached rows immediately —
    the TTL alone would leave a window in which our own writes are invisible.

    Implementations remove every cached language row of each shared_id: row
    fields are per-locale, but the denormalized documents/attachments and the
    bidirectional relations in a fetched raw are shared across the
    shared_id's rows, so invalidating one language would leave the others
    stale. File rows are not entity rows (a raw's documents/attachments are a
    runtime JOIN from the files collection by sharedId), so a files-collection
    mutation (a delete, a re-upload) invalidates the OWNING ENTITY's raws plus
    the affected files' cached bytes — the deleting/uploading seams know both
    keys and drive them through this port.
    """

    @abstractmethod
    def invalidate_entities(self, shared_ids: Sequence[str]) -> None:
        """Drop all cached raws for ``shared_ids`` (no-op for unknown ids)."""
        ...

    @abstractmethod
    def invalidate_files(self, filenames: Sequence[str]) -> None:
        """Drop the cached bytes of ``filenames`` (no-op for unknown names).

        Byte keys are immutable per storage filename, so eviction is only ever
        needed when the file itself is GONE (our own deletes): leaving a
        deleted file's bytes cached would let a stale raw's ghost ref
        "confirm identity" and re-attempt a finished delete. The bytes are
        persisted to the run's backup store BEFORE the delete call, so
        evicting after the delete can never lose anything.
        """
