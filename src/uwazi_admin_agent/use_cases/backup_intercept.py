"""The backup intercept — decorate CRUD helpers so real execution backs up by intercept (§2.4, Phase 4).

With a free-form script the touch set is emergent, so each mutating CRUD call
snapshots the raw before-state of the affected entity *before* applying. This
class wraps the reused sync CRUD helpers (from ``_build_sync_crud_functions``)
with that backup-before-apply semantics, populating
:class:`FilesystemBackupStore` + :class:`MigrationManifest` by construction.

The pure decision logic lives in :mod:`uwazi_admin_agent.domain.backup_decision`
(``decide_backup`` + ``populate_manifest`` + ``build_rewired_relationships``);
this class does the I/O: fetches raws via :class:`EntityRepositoryPort`, saves
snapshots via :class:`BackupStorePort`, and delegates to the underlying CRUD
helper. Not unit-tested (needs ports); validated via the simulation run.

This is also the write-path cache hook for sandbox CRUD writes: those go
through ``EntityApiPort`` (not the raw repository), so this intercept is the
only place that learns which entities a script mutated. Every intercepted
mutating op drops the affected entities' cached raws via
:class:`CacheInvalidationPort` right AFTER the underlying write (a failed
write changes nothing and invalidates nothing), keeping later reads honest.
File-row mutations ride the same hook: file rows are not entity rows (a
raw's ``documents``/``attachments`` are a runtime JOIN from the files
collection by sharedId), so a file DELETE is invisible to the entity-row
write paths — ``_record_deleted_files`` is the seam that invalidates the
affected entities' cached raws and evicts the deleted files' cached bytes
(the bytes are already in the backup store, so eviction is lossless).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from uwazi_admin_agent.configuration import ENTITY_CACHE_FRESH_SNAPSHOTS
from uwazi_admin_agent.domain.audit_record import AuditOutcome, AuditStep, make_audit_record
from uwazi_admin_agent.domain.backup_decision import (
    BackupDecision,
    build_rewired_relationships,
    decide_backup,
    populate_manifest,
)
from uwazi_admin_agent.domain.cap_enforcement import enforce_cap
from uwazi_admin_agent.domain.deleted_file import DeletedFile
from uwazi_admin_agent.domain.file_restore import extract_file_refs
from uwazi_admin_agent.domain.manifest import MigrationManifest
from uwazi_admin_agent.domain.snapshot import EntitySnapshot, FileRef
from uwazi_admin_agent.ports.audit_log_port import AuditLogPort
from uwazi_admin_agent.ports.backup_store_port import BackupStorePort
from uwazi_admin_agent.ports.cache_invalidation_port import CacheInvalidationPort
from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort
from uwazi_admin_agent.ports.file_repository_port import FileRepositoryPort


class BackupIntercept:
    """Wrap CRUD helpers so every mutation snapshots before-state by intercept.

    Constructed per real-execution run with the live ports + the run's
    manifest. ``decorate(crud)`` returns the intercepted write-helper dict
    ready to bind into the exec namespace. Maintains ``_backed_up`` (the
    first-touch set) so only the first operation on each entity snapshots.
    """

    def __init__(
        self,
        entity_repository: EntityRepositoryPort,
        backup_store: BackupStorePort,
        manifest: MigrationManifest,
        run_id: str,
        language: str,
        loop: asyncio.AbstractEventLoop | None,
        audit_log: AuditLogPort | None = None,
        cap: int = 1000,
        file_repository: FileRepositoryPort | None = None,
        cache_control: CacheInvalidationPort | None = None,
    ) -> None:
        self._entity_repository: EntityRepositoryPort = entity_repository
        self._backup_store: BackupStorePort = backup_store
        self._manifest: MigrationManifest = manifest
        self._run_id: str = run_id
        self._language: str = language
        self._loop: asyncio.AbstractEventLoop = loop
        self._backed_up: set[str] = set()
        self._audit_log: AuditLogPort | None = audit_log
        self._cap: int = cap
        self._file_repository: FileRepositoryPort | None = file_repository
        self._cache_control: CacheInvalidationPort | None = cache_control
        # Snapshot correctness beats speed: with the default on, _fetch_raws
        # drops the snapshotted entities' cached raws first so every backup
        # captures live server truth, not the TTL window (see configuration).
        self._snapshot_fresh: bool = ENTITY_CACHE_FRESH_SNAPSHOTS

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Set the event loop used for raw fetches (called from the worker thread)."""
        self._loop = loop

    @property
    def manifest(self) -> MigrationManifest:
        """The live manifest, populated as the script runs."""
        return self._manifest

    def decorate(self, crud: tuple) -> dict[str, Any]:
        """Wrap the reused sync CRUD tuple with backup-before-apply."""
        create_entities = crud[0]
        update_entities = crud[1]
        delete_entities = crud[2]
        publish_entities = crud[3]
        unpublish_entities = crud[4]
        set_publish_status = crud[5]
        create_relationships = crud[6]

        def create_entities_intercepted(entities_dicts: list[dict], language: str | None = None) -> list[dict]:
            results = create_entities(entities_dicts, language)
            created_ids = [sid for r in results if isinstance(r, dict) if r.get("success") if (sid := r.get("shared_id"))]
            self._record_created(results)
            self._emit("create", created_ids)
            return results

        def update_entities_intercepted(entities_dicts: list[dict], language: str | None = None) -> list[dict]:
            ids = [sid for e in entities_dicts if (sid := e.get("shared_id"))]
            self._backup_before_modify(ids, language or self._language)
            result = update_entities(entities_dicts, language)
            self._invalidate(ids)
            self._emit("update", ids)
            return result

        def delete_entities_intercepted(shared_ids: list[str], language: str | None = None) -> list[dict]:
            del language  # ignored: delete is by sharedId across ALL language rows
            self._backup_before_delete(shared_ids)
            result = delete_entities(shared_ids)
            self._invalidate(shared_ids)
            self._emit("delete", shared_ids)
            return result

        def set_publish_status_intercepted(
            shared_ids: list[str], published: bool, language: str | None = None
        ) -> list[dict]:
            del language  # ignored: publish/unpublish act on all language rows by sharedId
            self._backup_before_modify(shared_ids, self._language)
            result = set_publish_status(shared_ids, published)
            self._invalidate(shared_ids)
            self._emit("set_publish_status", shared_ids)
            return result

        def publish_entities_intercepted(shared_ids: list[str], language: str | None = None) -> dict:
            del language
            self._backup_before_modify(shared_ids, self._language)
            result = publish_entities(shared_ids)
            self._invalidate(shared_ids)
            self._emit("publish", shared_ids)
            return result

        def unpublish_entities_intercepted(shared_ids: list[str], language: str | None = None) -> dict:
            del language
            self._backup_before_modify(shared_ids, self._language)
            result = unpublish_entities(shared_ids)
            self._invalidate(shared_ids)
            self._emit("unpublish", shared_ids)
            return result

        def create_relationships_intercepted(relationships_dicts: list[dict], language: str | None = None) -> list[dict]:
            from_ids = [sid for r in relationships_dicts if (sid := r.get("from_entity_shared_id"))]
            to_ids = [sid for r in relationships_dicts if (sid := r.get("to_entity_shared_id"))]
            self._backup_before_rewire(from_ids, language or self._language)
            result = create_relationships(relationships_dicts, language)
            # Relations denormalize onto BOTH endpoints' raws — invalidate both.
            self._invalidate(from_ids + to_ids)
            self._emit("create_relationships", from_ids)
            return result

        return {
            "create_entities": create_entities_intercepted,
            "update_entities": update_entities_intercepted,
            "delete_entities": delete_entities_intercepted,
            "publish_entities": publish_entities_intercepted,
            "unpublish_entities": unpublish_entities_intercepted,
            "set_publish_status": set_publish_status_intercepted,
            "create_relationships": create_relationships_intercepted,
        }

    def _backup_before_modify(self, shared_ids: list[str], language: str) -> None:
        """Snapshot first-touch entities, add to manifest.modified."""
        decision = decide_backup("update", shared_ids, self._created_set(), self._backed_up)
        self._apply_snapshot_decision(decision, language)

    def _backup_before_delete(self, shared_ids: list[str]) -> None:
        """Snapshot first-touch entities, add to manifest.deleted (or remove from created).

        For a delete, Uwazi tears down the entity's stored file bytes
        (``BulkCleanupEntityUseCase`` → ``deleteEntityFiles`` →
        ``deleteFilesFromStorage``), so this is the **only** path that captures
        file bytes into the backup (modified/rewire paths keep the entity and its
        files). For each first-touch deleted entity, after fetching the raw and
        building the snapshot, capture the uploaded files' bytes via
        :class:`FileRepositoryPort` and persist them via :class:`BackupStorePort`,
        attaching the :class:`FileRef` metadata list to the snapshot. Best-effort:
        a fetch failure drops that one file from the snapshot's ``files`` (revert
        will not try to re-upload bytes it does not have); the snapshot still saves.
        """
        decision = decide_backup("delete", shared_ids, self._created_set(), self._backed_up)
        self._apply_snapshot_decision(decision, self._language, capture_files=True)

    def _backup_before_rewire(self, from_ids: list[str], language: str) -> None:
        """Snapshot first-touch from-entities, add to modified, record RewiredRelationship."""
        decision = decide_backup("create_relationships", from_ids, self._created_set(), self._backed_up)
        raws = self._fetch_raws(decision.snapshot_ids, language)
        snapshots = self._save_snapshots(decision.snapshot_ids, raws)
        rewired = build_rewired_relationships(decision.snapshot_ids, raws, language)
        _ = populate_manifest(self._manifest, decision, snapshots)
        self._manifest.rewired.extend(rewired)
        self._enforce_cap()
        logger.debug("backup rewire: snapshotted {} from-entities, recorded {} rewired", len(snapshots), len(rewired))

    def _apply_snapshot_decision(self, decision: BackupDecision, language: str, capture_files: bool = False) -> None:
        """Fetch raws, save snapshots, populate manifest for a backup decision.

        ``capture_files`` is set only on the delete path: after building each
        snapshot, fetch the uploaded files' bytes and attach the :class:`FileRef`
        metadata list so delete-revert can re-upload them.
        """
        raws = self._fetch_raws(decision.snapshot_ids, language)
        snapshots = self._save_snapshots(decision.snapshot_ids, raws, capture_files=capture_files)
        _ = populate_manifest(self._manifest, decision, snapshots)
        self._enforce_cap()
        logger.debug("backup decision: snapshotted {} entities", len(snapshots))

    def _record_created(self, results: list[dict]) -> None:
        """Record script-created shared_ids onto manifest.created (post-create)."""
        new_ids = [sid for r in results if isinstance(r, dict) if (sid := r.get("shared_id")) if r.get("success")]
        if not new_ids:
            return
        decision = BackupDecision(add_created=new_ids)
        _ = populate_manifest(self._manifest, decision, snapshots={})
        self._enforce_cap()
        logger.debug("backup intercept: recorded {} created entities", len(new_ids))

    def _enforce_cap(self) -> None:
        """Raise :class:`CapExceededError` if the touch set exceeds the cap.

        Emits a ``cap_exceeded`` audit record (outcome=FAILURE) before raising
        so the audit log shows why the run halted. The raised error propagates
        as a script error and triggers the on-error policy.
        """
        try:
            enforce_cap(self._manifest, self._cap)
        except Exception as exc:
            self._emit("cap_exceeded", [], outcome=AuditOutcome.FAILURE, detail=str(exc))
            raise

    def _emit(
        self,
        op_kind: str,
        shared_ids: list[str],
        outcome: AuditOutcome = AuditOutcome.SUCCESS,
        detail: str | None = None,
    ) -> None:
        """Append one audit record for an intercepted op (no-op if no audit log)."""
        if self._audit_log is None:
            return
        self._audit_log.append(
            self._run_id,
            make_audit_record(
                run_id=self._run_id,
                step=AuditStep.EXECUTE,
                op_kind=op_kind,
                shared_ids=shared_ids,
                outcome=outcome,
                detail=detail,
            ),
        )

    def _fetch_raws(self, shared_ids: list[str], language: str) -> dict[str, dict[str, Any]]:
        """Fetch the full raw (with relations) for each id via the entity repository.

        Snapshot correctness beats speed: when ``ENTITY_CACHE_FRESH_SNAPSHOTS``
        is on (the default), each snapshotted entity's cached raws are dropped
        first so the backup comes from the live instance rather than the TTL
        window — the refetch then repopulates the cache, and the post-write
        invalidation clears that again, so the loop is self-correcting.
        """
        if self._snapshot_fresh:
            self._invalidate(shared_ids)
        raws: dict[str, dict[str, Any]] = {}
        for sid in shared_ids:
            raws[sid] = self._loop.run_until_complete(self._entity_repository.get_raw_by_shared_id(sid, language))
        return raws

    def _save_snapshots(
        self, shared_ids: list[str], raws: dict[str, dict[str, Any]], capture_files: bool = False
    ) -> dict[str, EntitySnapshot]:
        """Build + persist EntitySnapshots; mark ids as backed up.

        When ``capture_files`` is set (delete path), also capture the uploaded
        files' bytes for each entity and attach the :class:`FileRef` metadata
        list to the snapshot (best-effort per file). The bytes live in the backup
        store as parallel binary artifacts; the snapshot JSON carries only the
        metadata, so raw fidelity (§2.5) is preserved and the snapshot stays
        human-readable.
        """
        snapshots: dict[str, EntitySnapshot] = {}
        now = datetime.now(timezone.utc)
        for sid in shared_ids:
            raw = raws[sid]
            file_refs = self._capture_files(sid, raw) if capture_files else None
            snap = EntitySnapshot(
                shared_id=sid,
                internal_id=raw.get("_id"),
                language=raw.get("language"),
                raw=raw,
                captured_at=now,
                files=file_refs,
            )
            self._backup_store.save_snapshot(self._run_id, snap)
            snapshots[sid] = snap
            self._backed_up.add(sid)
        return snapshots

    def _capture_files(self, shared_id: str, raw: dict[str, Any]) -> list[FileRef]:
        """Fetch + persist the uploaded files for a to-be-deleted entity.

        Returns the :class:`FileRef` metadata list to attach to the snapshot. If
        no file repository is wired (e.g. tests), returns an empty list so the
        snapshot records "no files captured" rather than ``None`` (``None`` means
        "not a delete-path snapshot"; an empty list means "delete-path, no files").
        Best-effort: a fetch failure for one file drops that file from the list
        and logs a warning; the snapshot still saves with the remaining refs.
        """
        if self._file_repository is None:
            return []
        refs = extract_file_refs(raw)
        captured: list[FileRef] = []
        for ref in refs:
            data = self._loop.run_until_complete(self._file_repository.get_file_bytes(ref.filename))
            if data is None:
                logger.warning("backup: file bytes not found sharedId={} filename={}", shared_id, ref.filename)
                continue
            self._backup_store.save_file_bytes(self._run_id, shared_id, ref.file_id, data)
            captured.append(ref)
        logger.debug("backup: captured {}/{} files sharedId={}", len(captured), len(refs), shared_id)
        return captured

    def _created_set(self) -> set[str]:
        """The set of shared_ids currently in manifest.created."""
        return {e.shared_id for e in self._manifest.created}

    def _save_file_backup(self, shared_id: str, file_id: str, data: bytes) -> None:
        """Persist one to-be-deleted file's bytes BEFORE the delete call.

        The deletion core's revert precondition: called on the WORKER thread
        with BYTES IN HAND (store writes are sync filesystem I/O — the worker
        cannot borrow the script's event loop), keyed
        ``(run_id, shared_id, file_id)`` exactly like the delete-path capture.
        The bytes of refused/soft-failed targets stay in the store as harmless
        orphans (``clear_run`` wipes them on re-execute). File rows are not
        entity writes, so NO entity snapshot is taken here — the entity raw
        is untouched by a file delete. The CACHE consequences (raw + byte
        eviction) are owned by :meth:`_record_deleted_files`, which runs only
        for the deletes that actually succeeded.
        """
        self._backup_store.save_file_bytes(self._run_id, shared_id, file_id, data)

    def _record_deleted_files(self, records: list[DeletedFile]) -> None:
        """Record successful file-deletes on the manifest + invalidate caches (script thread).

        Called by the bound file-deletion helpers AFTER the deletion batch
        joins — manifest writes never race the worker tasks (the same
        invariant the write helpers keep) — with one :class:`DeletedFile` per
        delete that SUCCEEDED (a soft-``False`` delete left the file in place;
        recording it would make revert re-upload a copy that never went away,
        a duplicate). Then the caches are dropped so the files-collection
        mutation is visible to the very next read: the affected entities'
        cached raws (all language rows — the JOIN view is shared across
        locales) and the deleted files' cached bytes (a stale raw's ghost ref
        plus its still-cached bytes would let a re-run re-attempt the finished
        delete). Cache eviction is lossless by construction — the cache is a
        read-through mirror, the truth is Uwazi + this run's backup store
        (the bytes were persisted BEFORE each delete), and entries repopulate
        lazily on the next read.

        Invalidation runs BEFORE the cap check (distinct entities — see
        :func:`touch_set_count`) and any hard-error re-raise (the helpers call
        this before ``_raise_first_write_error``), so a partial or
        cap-tripping batch leaves its APPLIED deletes recorded AND
        cache-fresh — revertable and re-runnable. The ``delete_file`` audit
        record still closes the method.
        """
        if not records:
            return
        self._manifest.deleted_files.extend(records)
        self._invalidate(sorted({r.shared_id for r in records}))
        self._invalidate_file_bytes([r.filename for r in records])
        self._enforce_cap()
        self._emit("delete_file", sorted({r.shared_id for r in records}))
        logger.debug(
            "backup intercept: recorded {} deleted file(s) on {} entity(ies)",
            len(records),
            len({r.shared_id for r in records}),
        )

    def _invalidate(self, shared_ids: list[str]) -> None:
        """Drop cached raws for ``shared_ids`` (no-op without a wired cache control).

        Script-created entities are never invalidated: a create mints a fresh
        sharedId nothing can be cached under yet.
        """
        if self._cache_control is None or not shared_ids:
            return
        self._cache_control.invalidate_entities(shared_ids)

    def _invalidate_file_bytes(self, filenames: list[str]) -> None:
        """Evict cached bytes of deleted files (no-op without a cache control).

        Only the delete seam calls this, and only for deletes that SUCCEEDED
        (their bytes are already in the run's backup store — eviction is
        lossless; a soft-failed delete left its file in place, so its cached
        bytes stay valid).
        """
        if self._cache_control is None or not filenames:
            return
        self._cache_control.invalidate_files(filenames)
