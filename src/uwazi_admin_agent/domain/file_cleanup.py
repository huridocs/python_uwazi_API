"""Pure duplicate-file cleanup decisions for per-entity dedupe.

Why this exists: Uwazi never dedupes uploads (every upload mints a fresh file
row joined to the entity by ``sharedId`` — see ``app/api/files/files.ts`` and
``FileUploadForEntity.ts``), so a merge that re-uploaded each source's copy of
the same files leaves MULTIPLIED file rows on the target: the same document
N times, the same HTML attachment N times. The merge file-move now SKIPS
byte-identical re-uploads (:mod:`...domain.file_dedupe`), but targets merged
BEFORE that fix still carry the duplicates — and reverting an entity raw
cannot remove them (``documents``/``attachments`` are a runtime JOIN from the
files collection, not fields on the entity row; see ``entities.js``
``withDocuments``). This module is the pure decision that lets a generated
script delete the redundant FILE rows.

The no-loss contract (the operator's #1 constraint):

- A file is deleted ONLY when another byte-identical (sha256) copy remains on
  the SAME entity — never the last copy of anything, never across entities,
  never on (name, size) similarity alone (that key only nominates candidates
  in the move flow; here identity is ALWAYS the digest).
- Documents and attachments dedupe SEPARATELY: the same bytes sitting in the
  document slot vs. an attachment slot are structurally different rows, and
  deleting across kinds would reshape the entity, not just remove a redundant
  copy. Each (kind, digest) group keeps its own first member.
- A file whose bytes cannot be fetched (digest ``None``) is identity-
  UNCONFIRMED and is never deleted.
- A copy that one of the entity's own relationship connections cites is NEVER
  deleted: Uwazi's file delete tears down connections citing the deleted file
  (``FilesService.delete`` → ``relV1DS.deleteByFiles``), destroying the
  reference — a real loss. Such redundant copies stay visible and are
  reported as ``kept_cited`` so the operator can rewire them by hand. (V2
  text references are not visible in the raw; that residual risk is covered
  by the dry-run review, not by this guard.)

Pure: no I/O — the caller fetches raws/bytes and hands in the digests.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from uwazi_admin_agent.domain.snapshot import FileRef


class CleanupPlan(BaseModel):
    """What one entity's duplicate-file cleanup would remove (pure decision output).

    ``to_delete`` are the redundant copies safe to delete (a byte-identical
    survivor remains on the same entity, in the same kind slot, and no
    connection cites them); ``kept_cited`` are the redundant copies a
    connection cites — kept, but surfaced so the report can flag them. The
    keeper of each group is implicit: it is simply not in either list.
    """

    model_config = ConfigDict(frozen=True)

    to_delete: list[FileRef] = Field(default_factory=list, description="Redundant copies to delete, in raw order.")
    kept_cited: list[FileRef] = Field(
        default_factory=list,
        description="Redundant copies kept because a connection cites them (visible leftovers).",
    )


def cited_file_ids(raw: dict[str, Any]) -> set[str]:
    """The file ids the entity's own relationship connections cite.

    Uwazi joins each connection's fields onto the raw's ``relations``
    (``relationships.getByDocument`` → ``{template: null, entityData, ...relationship}``),
    so a relation entry's ``file`` is the ObjectId of the file it references —
    a plain hex string in the API JSON (an extended-JSON ``{"$oid": ...}``
    object is tolerated too). Deleting a cited file would tear that
    connection down with it, which is exactly what :func:`plan_entity_cleanup`
    refuses to do.
    """
    relations = raw.get("relations")
    if not isinstance(relations, list):
        return set()
    cited: set[str] = set()
    for relation in relations:
        if not isinstance(relation, dict):
            continue
        cited |= _file_id_values(relation.get("file"))
    return cited


def plan_entity_cleanup(refs: list[FileRef], digests: dict[str, str | None], cited: set[str]) -> CleanupPlan:
    """Decide which of ONE entity's uploaded files are redundant duplicates.

    ``digests`` maps ``ref.file_id`` → sha256 hex (``None`` = bytes
    unfetchable → identity unconfirmed → the file is KEPT). Files group by
    ``(kind, digest)``; a group of one has nothing redundant. Within a group
    of more than one: the FIRST ref in ``refs`` order (documents-then-
    attachments, then the raw's own order — :func:`extract_file_refs`) is the
    keeper, and every later member is deleted — unless a connection cites it
    (``cited``), in which case it is kept and reported. Keep-first is
    language-neutral for byte-identical PDFs (the content-detected language is
    a function of the bytes); only byte-identical attachments uploaded under
    different locales can carry different row languages, an accepted edge.
    Pure: no I/O. The output order is deterministic: duplicate groups in
    first-member order, each group's redundant members in refs order.
    """
    groups: dict[tuple[str, str], list[FileRef]] = {}
    for ref in refs:
        digest = digests.get(ref.file_id)
        if digest is None:
            continue
        groups.setdefault((ref.kind, digest), []).append(ref)
    to_delete: list[FileRef] = []
    kept_cited: list[FileRef] = []
    for group in groups.values():
        for redundant in group[1:]:
            if redundant.file_id in cited:
                kept_cited.append(redundant)
            else:
                to_delete.append(redundant)
    return CleanupPlan(to_delete=to_delete, kept_cited=kept_cited)


def _file_id_values(file_ref: Any) -> set[str]:
    """Extract the file-id string(s) from one relation's ``file`` field."""
    if isinstance(file_ref, str) and file_ref:
        return {file_ref}
    if isinstance(file_ref, dict):
        oid = file_ref.get("$oid")
        return {oid} if isinstance(oid, str) and oid else set()
    return set()
