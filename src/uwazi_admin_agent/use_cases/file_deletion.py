"""The shared backup-before-delete core for BOTH file-deletion nominators.

Two nominators produce file-deletion targets; ONE core deletes them:

- **dedupe** (:mod:`...use_cases.file_cleanup`): safety-by-construction — a
  target is only ever a byte-identical redundant copy whose survivor stays
  on the same entity (kind-separated groups, keep-first, cited kept,
  unfetchable kept). Its discovery ALREADY fetches every file's bytes to
  hash them, so it hands the core bytes in hand at zero extra GETs.
- **explicit deletion** (``delete_entity_files_parallel``): the operator
  names the files (``file_id`` precisely, or ``originalname``+``kind`` with
  an ambiguity refusal). Resolution + refusal rules are pure
  (:mod:`...domain.file_deletion`); the flow layer adds one GET per target
  so the core receives bytes in hand.

The core (:func:`delete_files_with_backup`) is the no-loss invariant made
explicit, per file, in order:

1. persist the bytes via the backup seam — BYTES IN HAND, so the seam is a
   plain sync call the WORKER thread can make (store writes are sync
   filesystem I/O; the worker cannot borrow the script's event loop);
2. only THEN call ``delete_file`` — a crash between the delete and the
   backup would be unrecoverable, the reverse order is merely wasteful.

Soft failures (bool-``False`` deletes) never raise: they count as ``failed``
and the file row stays (a re-run retries it idempotently). A hard port
EXCEPTION propagates so the caller's error policy decides — for both bound
helpers that is the ``_raise_first_write_error`` pattern: the script dies
loudly BEFORE any further deletes (the slower-but-safe choice).

Manifest recording happens on the SCRIPT thread AFTER the deletion batch
joins (see the helpers): each task's per-call accumulator (built on the
script thread, appended by the core right after each successful delete, and
therefore intact even when the task later raises) supplies the refs, and
only the SUCCESSFUL deletes are appended to
:attr:`MigrationManifest.deleted_files` — a soft-``False`` delete left the
file in place, and recording it would make revert re-upload a copy that
never went away (a duplicate). Recording runs BEFORE any hard error is
re-raised, so a partial batch stays revertable. Bytes saved for
refused/failed targets stay in the store as harmless orphans
(``clear_run`` wipes them on re-execute).

The recording seam (``BackupIntercept._record_deleted_files``) ALSO drops
the caches: the affected entities' cached raws and the deleted files'
cached bytes (invalidate-then-refetch — eviction is lossless by
construction; the bytes were persisted to the backup store BEFORE the
delete). This is what makes a delete visible to the very next read despite
file rows not being entity rows: without it, a re-run (or a new task's
discovery) would read a stale raw, see dead file_ids, and miss the files a
revert restored.

The await style is injected as ``run`` so the core matches the movers:
``asyncio.run`` on the parallel helpers' worker threads.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from uwazi_admin_agent.domain.file_cleanup import CleanupPlan, cited_file_ids, plan_entity_cleanup
from uwazi_admin_agent.domain.file_dedupe import file_digest
from uwazi_admin_agent.domain.file_deletion import ResolvedDeletions, refuse_unbackable_targets, resolve_deletion_requests
from uwazi_admin_agent.domain.file_restore import extract_file_refs
from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort
from uwazi_admin_agent.ports.file_repository_port import FileRepositoryPort
from uwazi_admin_agent.use_cases.file_cleanup import CoroRunner

# The backup seam: (shared_id, file_id, data) -> None. Persists one file's
# bytes BEFORE the delete call (the revert precondition). Built by the bound
# helpers from the BackupIntercept (the manifest/backup-store owner); typed
# as a plain callable so the core stays decoupled from the intercept module.
FileBackupSaver = Callable[[str, str, bytes], None]


def delete_files_with_backup(
    file_repository: FileRepositoryPort,
    save_bytes: FileBackupSaver,
    shared_id: str,
    ref_bytes: list[tuple[Any, bytes]],
    run: CoroRunner,
    applied: list[Any],
) -> list[tuple[Any, bool]]:
    """Delete files with bytes-in-hand backup, STRICTLY in the safe order.

    Per ``(ref, data)``: persist ``data`` via ``save_bytes`` FIRST, then call
    ``delete_file``; a ``True`` answer appends ``ref`` to ``applied`` right
    then (the accumulator is built on the SCRIPT thread and outlives a task
    exception, so a hard port error mid-task cannot orphan deletes that
    already applied); a ``False`` answer counts the pair as ``(ref, False)``
    and never raises (the file row simply stayed). A hard port EXCEPTION
    propagates — the bytes of the file it was deleting are already persisted,
    so the caller's re-raise leaves the delete REVERTABLE even mid-crash.
    Returns one ``(ref, deleted?)`` pair per file. ``ref`` is typed ``Any`` to
    match the flow layer's ``FileRef``-shaped objects without importing the
    domain model here twice.
    """
    outcomes: list[tuple[Any, bool]] = []
    for ref, data in ref_bytes:
        save_bytes(shared_id, ref.file_id, data)
        deleted = bool(run(file_repository.delete_file(ref.file_id)))
        if deleted:
            applied.append(ref)
        outcomes.append((ref, deleted))
    return outcomes


def cleanup_plan_with_bytes(
    entity_repository: EntityRepositoryPort,
    file_repository: FileRepositoryPort,
    shared_id: str,
    lang: str,
    run: CoroRunner,
) -> tuple[CleanupPlan, dict[str, bytes]]:
    """Discover ONE entity's dedupe plan, KEEPING the fetched bytes.

    Identical reads to the historical ``cleanup_plan_for_entity`` (raw →
    cited ids → refs → bytes → digests → the pure
    :func:`plan_entity_cleanup` decision — the byte fetch per ref is what
    confirms identity), but the fetched bytes are RETURNED instead of
    dropped: the deletion core backs them up before deleting, at zero extra
    GETs (dedupe is the nominator that already held every file's bytes).
    """
    raw = run(entity_repository.get_raw_by_shared_id(shared_id, lang))
    refs = extract_file_refs(raw)
    bytes_by_id: dict[str, bytes] = {}
    digests: dict[str, str | None] = {}
    for ref in refs:
        data = run(file_repository.get_file_bytes(ref.filename))
        if data is not None:
            bytes_by_id[ref.file_id] = data
            digests[ref.file_id] = file_digest(data)
        else:
            digests[ref.file_id] = None
    return plan_entity_cleanup(refs, digests, cited_file_ids(raw)), bytes_by_id


def plan_explicit_deletions(
    entity_repository: EntityRepositoryPort,
    file_repository: FileRepositoryPort,
    shared_id: str,
    lang: str,
    deletions: list[dict[str, Any]],
    run: CoroRunner,
) -> tuple[ResolvedDeletions, dict[str, bytes]]:
    """Discover ONE entity's explicit deletion plan (reads only).

    Fetches the raw, resolves the requests against the entity's refs (pure
    :func:`resolve_deletion_requests` — ``file_id`` precisely, names with an
    ambiguity refusal, cited copies refused), then fetches each target's
    bytes so the deletion core receives bytes in hand. A target whose bytes
    cannot be fetched becomes an ``unavailable`` refusal: backup-before-delete
    is mandatory, and a file the core cannot back up is a file it will not
    delete.
    """
    raw = run(entity_repository.get_raw_by_shared_id(shared_id, lang))
    refs = extract_file_refs(raw)
    resolved = resolve_deletion_requests(shared_id, refs, cited_file_ids(raw), deletions)
    bytes_by_id: dict[str, bytes] = {}
    for ref in resolved.targets:
        data = run(file_repository.get_file_bytes(ref.filename))
        if data is not None:
            bytes_by_id[ref.file_id] = data
    fetched = {ref.file_id: bytes_by_id.get(ref.file_id) for ref in resolved.targets}
    return refuse_unbackable_targets(shared_id, resolved, fetched), bytes_by_id
