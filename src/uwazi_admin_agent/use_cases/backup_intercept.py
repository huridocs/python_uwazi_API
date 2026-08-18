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
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from uwazi_admin_agent.domain.audit_record import AuditOutcome, AuditStep, make_audit_record
from uwazi_admin_agent.domain.backup_decision import (
    BackupDecision,
    build_rewired_relationships,
    decide_backup,
    populate_manifest,
)
from uwazi_admin_agent.domain.cap_enforcement import enforce_cap
from uwazi_admin_agent.domain.manifest import MigrationManifest
from uwazi_admin_agent.domain.snapshot import EntitySnapshot
from uwazi_admin_agent.ports.audit_log_port import AuditLogPort
from uwazi_admin_agent.ports.backup_store_port import BackupStorePort
from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort


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
            self._emit("update", ids)
            return result

        def delete_entities_intercepted(shared_ids: list[str]) -> list[dict]:
            self._backup_before_delete(shared_ids)
            result = delete_entities(shared_ids)
            self._emit("delete", shared_ids)
            return result

        def set_publish_status_intercepted(shared_ids: list[str], published: bool) -> list[dict]:
            self._backup_before_modify(shared_ids, self._language)
            result = set_publish_status(shared_ids, published)
            self._emit("set_publish_status", shared_ids)
            return result

        def publish_entities_intercepted(shared_ids: list[str]) -> dict:
            self._backup_before_modify(shared_ids, self._language)
            result = publish_entities(shared_ids)
            self._emit("publish", shared_ids)
            return result

        def unpublish_entities_intercepted(shared_ids: list[str]) -> dict:
            self._backup_before_modify(shared_ids, self._language)
            result = unpublish_entities(shared_ids)
            self._emit("unpublish", shared_ids)
            return result

        def create_relationships_intercepted(relationships_dicts: list[dict], language: str | None = None) -> list[dict]:
            from_ids = [sid for r in relationships_dicts if (sid := r.get("from_entity_shared_id"))]
            self._backup_before_rewire(from_ids, language or self._language)
            result = create_relationships(relationships_dicts, language)
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
        """Snapshot first-touch entities, add to manifest.deleted (or remove from created)."""
        decision = decide_backup("delete", shared_ids, self._created_set(), self._backed_up)
        self._apply_snapshot_decision(decision, self._language)

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

    def _apply_snapshot_decision(self, decision: BackupDecision, language: str) -> None:
        """Fetch raws, save snapshots, populate manifest for a backup decision."""
        raws = self._fetch_raws(decision.snapshot_ids, language)
        snapshots = self._save_snapshots(decision.snapshot_ids, raws)
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
        """Fetch the full raw (with relations) for each id via the entity repository."""
        raws: dict[str, dict[str, Any]] = {}
        for sid in shared_ids:
            raws[sid] = self._loop.run_until_complete(self._entity_repository.get_raw_by_shared_id(sid, language))
        return raws

    def _save_snapshots(self, shared_ids: list[str], raws: dict[str, dict[str, Any]]) -> dict[str, EntitySnapshot]:
        """Build + persist EntitySnapshots; mark ids as backed up."""
        snapshots: dict[str, EntitySnapshot] = {}
        now = datetime.now(timezone.utc)
        for sid in shared_ids:
            raw = raws[sid]
            snap = EntitySnapshot(
                shared_id=sid,
                internal_id=raw.get("_id"),
                language=raw.get("language"),
                raw=raw,
                captured_at=now,
            )
            self._backup_store.save_snapshot(self._run_id, snap)
            snapshots[sid] = snap
            self._backed_up.add(sid)
        return snapshots

    def _created_set(self) -> set[str]:
        """The set of shared_ids currently in manifest.created."""
        return {e.shared_id for e in self._manifest.created}
