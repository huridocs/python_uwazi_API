"""Pure duplicate-file identity for the merge file-move.

Why this exists: Uwazi never dedupes uploads — every upload inserts a fresh
file row (a freshly minted storage filename) joined to the entity by
``sharedId``, and the raw's ``documents``/``attachments`` are exactly that
join (see ``app/api/entities/entities.js`` ``withDocuments``). Re-uploading a
file a merge source already shares with the target therefore MULTIPLIES it:
merging N duplicate entities that carry the same files leaves N copies on the
target. The movers now skip a source file whose BYTES are already on the
target instead of re-uploading it.

Identity is exact, never heuristical: the cheap
:func:`file_identity_key` ``(kind, originalname, size)`` only NOMINATES
candidate duplicates (so byte fetches are avoided for obviously-different
files); a skip is decided solely by a :func:`file_digest` (sha256) match. A
same-named, same-sized but different-content file is KEPT — the no-loss bias:
a false skip would silently destroy data (the sources are deleted right
after the move), a missed skip only leaves a visible duplicate.

Pure: no I/O — :class:`TargetFileDeduper` is bookkeeping only; the caller
fetches bytes and reports confirmed digests back.
"""

import hashlib
from collections import defaultdict

from uwazi_admin_agent.domain.snapshot import FileRef


def file_identity_key(ref: FileRef) -> tuple[str, str, int | None]:
    """The cheap duplicate-candidate key: (kind, originalname, size)."""
    return (ref.kind, ref.originalname, ref.size)


def file_digest(data: bytes) -> str:
    """The exact byte identity of file contents (sha256 hex digest)."""
    return hashlib.sha256(data).hexdigest()


class TargetFileDeduper:
    """Which file contents are already on ONE target entity (pure bookkeeping).

    Built from the target's refs — its raw's joined ``documents``/
    ``attachments`` list every file row Uwazi ever attached to the sharedId,
    in any language. Digests are confirmed lazily: :meth:`candidates` nominates
    same-key target files whose bytes the caller may fetch and compare;
    :meth:`remember` records a digest once it is known present (a
    byte-confirmed pre-existing file, or a file this move just uploaded).
    """

    def __init__(self, target_refs: list[FileRef]) -> None:
        self._by_key: dict[tuple[str, str, int | None], list[FileRef]] = defaultdict(list)
        for ref in target_refs:
            self._by_key[file_identity_key(ref)].append(ref)
        self._present: set[str] = set()

    def candidates(self, ref: FileRef) -> list[FileRef]:
        """The target files sharing ``ref``'s identity key — the only possible duplicates."""
        return list(self._by_key.get(file_identity_key(ref), ()))

    def has_digest(self, digest: str) -> bool:
        """True when this exact content is already known to be on the target."""
        return digest in self._present

    def remember(self, digest: str) -> None:
        """Record a digest as present on the target (byte-confirmed or just uploaded)."""
        self._present.add(digest)
