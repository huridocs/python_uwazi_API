"""The shared real file-move flow used by BOTH file movers (sequential + parallel).

One target's move, in order: build the :class:`TargetFileDeduper` from the
target's raw (the joined documents/attachments list every file row Uwazi ever
attached to that sharedId, in any language), then per source file —
sequentially, same-row safety — fetch its bytes, SKIP it when byte-identical
content is already on the target (a duplicate entity's copy of the same
PDF/HTML must not multiply on the target), else re-upload it (the sequential
mover's copy-then-delete semantics, minus the multiplication).

Soft failures (missing source bytes, rejected upload) count as ``failed`` and
never raise — the merge still deletes the sources, exactly like the
historical mover. A hard port EXCEPTION (source or target fetch, upload
transport) propagates so the caller's error policy decides — the run dies
BEFORE the merge's deletes and the sources keep their files.

The await style is injected as ``run`` so both movers share this flow:
``asyncio.run`` on the parallel mover's worker threads (the raw repositories
block per request, so they must run on their own thread), or
``loop.run_until_complete`` on the sequential mover's dedicated loop.

Dedupe scope: exact within ONE move (one call, one target — the parallel
helper additionally refuses a duplicated target per call). A SECOND call
against the same target re-fetches its raw and sees the first call's uploads
— unless the entity-raw cache serves a stale entry (uploads are not
entity-row writes, so they do not invalidate it); that edge can only
re-upload a duplicate, never lose one.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from uwazi_admin_agent.domain.file_dedupe import TargetFileDeduper, file_digest
from uwazi_admin_agent.domain.file_restore import extract_file_refs
from uwazi_admin_agent.domain.snapshot import FileRef
from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort
from uwazi_admin_agent.ports.file_repository_port import FileRepositoryPort

# Runs ONE coroutine to completion: asyncio.run or loop.run_until_complete.
CoroRunner = Callable[[Any], Any]

TransferOutcome = Literal["moved", "skipped", "failed"]


def move_files_for_target(
    entity_repository: EntityRepositoryPort,
    file_repository: FileRepositoryPort,
    from_shared_ids: list[str],
    to_shared_id: str,
    lang: str,
    run: CoroRunner,
) -> dict[str, int]:
    """Move every source file to ONE target — sequentially, skipping byte-duplicates.

    Returns the counts dict ``{"moved", "failed", "skipped"}`` — each mover
    shapes its own contract around it (the sequential helper returns it
    as-is; the parallel task wraps it with its ``to_shared_id``).
    ``skipped`` counts files whose bytes were already on the target (the
    target's own copy, an earlier source's identical copy in this same move,
    or a byte-confirmed pre-existing file), so a merge of duplicate entities
    leaves ONE copy of each unique file instead of N.
    """
    deduper = _target_deduper(entity_repository, to_shared_id, lang, run)
    counts: dict[str, int] = {"moved": 0, "skipped": 0, "failed": 0}
    for from_sid in from_shared_ids:
        raw = run(entity_repository.get_raw_by_shared_id(from_sid, lang))
        for ref in extract_file_refs(raw):
            outcome = transfer_file_with_dedupe(file_repository, ref, to_shared_id, lang, deduper, run)
            counts[outcome] += 1
    return counts


def transfer_file_with_dedupe(
    file_repository: FileRepositoryPort,
    ref: FileRef,
    to_shared_id: str,
    lang: str,
    deduper: TargetFileDeduper,
    run: CoroRunner,
) -> TransferOutcome:
    """Fetch one file's bytes; skip when identical content is already on the target; else upload."""
    data = run(file_repository.get_file_bytes(ref.filename))
    if data is None:
        return "failed"
    digest = file_digest(data)
    if deduper.has_digest(digest) or _target_already_has(file_repository, deduper, ref, digest, run):
        return "skipped"
    if _upload(file_repository, data, ref, to_shared_id, lang, run):
        deduper.remember(digest)  # later sources with the same bytes skip for free
        return "moved"
    return "failed"


def _target_deduper(
    entity_repository: EntityRepositoryPort,
    to_shared_id: str,
    lang: str,
    run: CoroRunner,
) -> TargetFileDeduper:
    """The dedupe index for one target: its raw's joined file refs."""
    raw = run(entity_repository.get_raw_by_shared_id(to_shared_id, lang))
    return TargetFileDeduper(extract_file_refs(raw))


def _target_already_has(
    file_repository: FileRepositoryPort,
    deduper: TargetFileDeduper,
    ref: FileRef,
    digest: str,
    run: CoroRunner,
) -> bool:
    """True when a same-key target file's fetched bytes hash to ``digest``.

    A candidate whose bytes cannot be fetched (``None``) is NOT a confirmed
    duplicate — the file uploads (no-loss bias). Missing target bytes are
    cached like any fetch, so repeated candidates cost one request total.
    """
    for candidate in deduper.candidates(ref):
        data = run(file_repository.get_file_bytes(candidate.filename))
        if data is not None and file_digest(data) == digest:
            deduper.remember(digest)
            return True
    return False


def _upload(
    file_repository: FileRepositoryPort,
    data: bytes,
    ref: FileRef,
    to_shared_id: str,
    lang: str,
    run: CoroRunner,
) -> bool:
    """Re-upload one file's bytes to the target (documents and attachments use distinct endpoints)."""
    upload_lang = ref.language or lang
    if ref.kind == "document":
        return bool(
            run(file_repository.upload_document(data, to_shared_id, upload_lang, ref.originalname, ref.content_type))
        )
    return bool(run(file_repository.upload_attachment(data, to_shared_id, upload_lang, ref.originalname, ref.content_type)))
