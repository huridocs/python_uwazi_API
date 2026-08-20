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


class RevertRunUseCase:
    """Revert a run: load manifest, build actions, execute restores/deletes in order."""

    def __init__(
        self,
        entity_repository: EntityRepositoryPort,
        backup_store: BackupStorePort,
        audit_log: AuditLogPort | None = None,
    ) -> None:
        self._entity_repository: EntityRepositoryPort = entity_repository
        self._backup_store: BackupStorePort = backup_store
        self._audit_log: AuditLogPort | None = audit_log

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
