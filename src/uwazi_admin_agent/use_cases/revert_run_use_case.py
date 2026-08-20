"""Revert a run by loading the manifest + snapshots and restoring exactly (§2.3, §2.6, Phase 4).

The production revert path: load the persisted manifest, call the pure
:func:`build_revert_actions` to get the ordered action list, and execute each
action via :class:`EntityRepositoryPort` (``save_raw`` for entity/relationship
restores, ``delete_by_shared_id`` for created-entity deletions). The ordering
(relationships → modified → deleted → delete-created-last) is guaranteed by
the builder; this use case is a thin orchestrator.

Phase 6 adds an **audit record** per revert action (step=REVERT) so the run's
audit log records the restore alongside the execute writes that produced it.
Optional injection keeps existing tests green; the runtime always wires a real
log.

Testable with an in-memory repo + in-memory backup store (the DoD's "with an
in-memory repo").
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from uwazi_admin_agent.domain.audit_record import AuditOutcome, AuditStep, make_audit_record
from uwazi_admin_agent.domain.file_restore import build_file_restore_actions
from uwazi_admin_agent.domain.manifest import RunStatus
from uwazi_admin_agent.domain.revert import (
    DeleteEntityAction,
    RecreateEntityAction,
    RestoreEntityAction,
    RestoreRelationshipAction,
    build_revert_actions,
)
from uwazi_admin_agent.domain.revert_gate import RevertRefusedError, decide_revert_gate
from uwazi_admin_agent.ports.audit_log_port import AuditLogPort
from uwazi_admin_agent.ports.backup_store_port import BackupStorePort
from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort
from uwazi_admin_agent.ports.file_repository_port import FileRepositoryPort


class RevertRunUseCase:
    """Revert a run: load manifest, build actions, execute restores/deletes in order."""

    def __init__(
        self,
        entity_repository: EntityRepositoryPort,
        backup_store: BackupStorePort,
        audit_log: AuditLogPort | None = None,
        file_repository: FileRepositoryPort | None = None,
    ) -> None:
        self._entity_repository: EntityRepositoryPort = entity_repository
        self._backup_store: BackupStorePort = backup_store
        self._audit_log: AuditLogPort | None = audit_log
        self._file_repository: FileRepositoryPort | None = file_repository

    async def revert(self, run_id: str) -> None:
        """Revert run ``run_id`` by restoring every backed-up entity and deleting created ones.

        Refuses an already-``REVERTED`` run (re-reverting a delete-run would
        re-create its deleted entities a second time, minting new sharedIds and
        leaking orphans — see :mod:`domain.revert_gate`).
        """
        manifest = self._backup_store.load_manifest(run_id)
        gate = decide_revert_gate(manifest.status)
        if gate.action == "refuse":
            raise RevertRefusedError(gate.reason or "revert refused")

        actions = build_revert_actions(manifest, lambda sid: self._backup_store.load_snapshot(run_id, sid))

        for action in actions:
            await self._execute_action(action, run_id, manifest)

        # Persist any restored_shared_id mappings recorded during re-creates so
        # post-revert verification can fetch the re-created rows by their new ids.
        self._backup_store.save_manifest(run_id, manifest)
        self._backup_store.update_status(run_id, RunStatus.REVERTED)
        self._emit_run(AuditOutcome.SUCCESS, run_id)
        logger.info(
            "revert done run={} actions={} modified={} deleted={} created={}",
            run_id,
            len(actions),
            len(manifest.modified),
            len(manifest.deleted),
            len(manifest.created),
        )

    async def _execute_action(self, action: Any, run_id: str, manifest: Any) -> None:
        """Dispatch one revert action to the entity repository (+ audit + manifest record)."""
        if isinstance(action, RestoreRelationshipAction):
            await self._restore_relationship(action, run_id)
        elif isinstance(action, RestoreEntityAction):
            await self._entity_repository.save_raw(action.snapshot.raw)
            self._emit(run_id, "restore_entity", [action.snapshot.shared_id])
            logger.debug("revert: restored entity sharedId={}", action.snapshot.shared_id)
        elif isinstance(action, RecreateEntityAction):
            new_shared_id = await self._entity_repository.create_raw(action.snapshot.raw)
            self._record_restored_shared_id(manifest, action.snapshot.shared_id, new_shared_id)
            self._emit(run_id, "recreate_entity", [new_shared_id])
            logger.info(
                "revert: re-created entity (old sharedId={} -> new sharedId={})",
                action.snapshot.shared_id,
                new_shared_id,
            )
            await self._restore_files(action.snapshot, new_shared_id, run_id)
        elif isinstance(action, DeleteEntityAction):
            await self._entity_repository.delete_by_shared_id(action.shared_id)
            self._emit(run_id, "delete_created", [action.shared_id])
            logger.debug("revert: deleted created entity sharedId={}", action.shared_id)

    @staticmethod
    def _record_restored_shared_id(manifest: Any, old_shared_id: str, new_shared_id: str) -> None:
        """Record the new sharedId on the matching deleted manifest entry.

        :class:`EntityIdentity` is frozen, so the entry is replaced in place with
        a copy carrying the new ``restored_shared_id`` (matched by its old
        ``shared_id``). Stored for post-revert verification + audit.
        """
        for idx, entry in enumerate(manifest.deleted):
            if entry.shared_id == old_shared_id:
                manifest.deleted[idx] = entry.model_copy(update={"restored_shared_id": new_shared_id})
                return
        logger.warning("revert: no manifest.deleted entry for old sharedId={}", old_shared_id)

    async def _restore_relationship(self, action: RestoreRelationshipAction, run_id: str) -> None:
        """Fetch the entity's current raw, patch the relationship field, save."""
        raw = await self._entity_repository.get_raw_by_shared_id(action.entity.shared_id, action.entity.language)
        raw[action.property_name] = action.before
        await self._entity_repository.save_raw(raw)
        self._emit(run_id, "restore_relationship", [action.entity.shared_id])
        logger.debug("revert: restored relationship sharedId={} property={}", action.entity.shared_id, action.property_name)

    async def _restore_files(self, snapshot: Any, new_shared_id: str, run_id: str) -> None:
        """Re-upload the snapshot's captured files to the re-created entity.

        Runs as a sub-step of :class:`RecreateEntityAction` *after* ``create_raw``
        returns the new ``sharedId`` (uploads target ``shared_id`` + ``language``).
        Best-effort: a failed upload is logged + recorded as a ``restore_file_failed``
        audit record but does NOT fail the revert — the entity is already re-created
        with its data; file gaps surface in post-revert verification. A missing file
        repository (e.g. tests) or a snapshot with no captured files is a no-op.
        """
        if self._file_repository is None or not snapshot.files:
            return
        old_shared_id = snapshot.shared_id
        actions = build_file_restore_actions(snapshot.files)
        for act in actions:
            try:
                data = self._backup_store.load_file_bytes(run_id, old_shared_id, act.file_id)
            except Exception as exc:  # noqa: BLE001 — best-effort; missing bytes must not abort revert
                self._emit_file(run_id, new_shared_id, failed=True, detail=f"load bytes: {exc}")
                logger.warning(
                    "revert: file bytes missing run={} old={} fileId={}: {}", run_id, old_shared_id, act.file_id, exc
                )
                continue
            ok = await self._upload_one(act, data, new_shared_id)
            if ok:
                self._emit_file(run_id, new_shared_id, failed=False)
                logger.debug("revert: re-uploaded file sharedId={} originalname={}", new_shared_id, act.originalname)
            else:
                self._emit_file(run_id, new_shared_id, failed=True, detail=act.originalname)
                logger.warning("revert: file upload failed sharedId={} originalname={}", new_shared_id, act.originalname)

    async def _upload_one(self, action: Any, data: bytes, new_shared_id: str) -> bool:
        """Dispatch one file-restore action to the file repository."""
        language = action.language
        title = action.originalname
        content_type = action.content_type
        if action.kind == "upload_document":
            return await self._file_repository.upload_document(data, new_shared_id, language, title, content_type)
        return await self._file_repository.upload_attachment(data, new_shared_id, language, title, content_type)

    def _emit_file(self, run_id: str, shared_id: str, *, failed: bool, detail: str | None = None) -> None:
        """Append one file-restore audit record (no-op if no audit log)."""
        if self._audit_log is None:
            return
        self._audit_log.append(
            run_id,
            make_audit_record(
                run_id=run_id,
                step=AuditStep.REVERT,
                op_kind="restore_file_failed" if failed else "restore_file",
                shared_ids=[shared_id],
                outcome=AuditOutcome.FAILURE if failed else AuditOutcome.SUCCESS,
                detail=detail,
            ),
        )

    def _emit(self, run_id: str, op_kind: str, shared_ids: list[str]) -> None:
        """Append one revert-action audit record (no-op if no audit log)."""
        if self._audit_log is None:
            return
        self._audit_log.append(
            run_id,
            make_audit_record(
                run_id=run_id,
                step=AuditStep.REVERT,
                op_kind=op_kind,
                shared_ids=shared_ids,
                outcome=AuditOutcome.SUCCESS,
            ),
        )

    def _emit_run(self, outcome: AuditOutcome, run_id: str) -> None:
        """Append a run-level ``revert`` audit record (no-op if no audit log)."""
        if self._audit_log is None:
            return
        self._audit_log.append(
            run_id,
            make_audit_record(
                run_id=run_id,
                step=AuditStep.REVERT,
                op_kind="revert",
                shared_ids=[],
                outcome=outcome,
            ),
        )
