"""Revert a run by loading the manifest + snapshots and restoring exactly (§2.3, §2.6, Phase 4).

The production revert path: load the persisted manifest, call the pure
:func:`build_revert_actions` to get the ordered action list, and execute each
action via :class:`EntityRepositoryPort` (``save_raw`` for entity/relationship
restores, ``create_raw`` for deleted-entity re-creates, ``delete_by_shared_id``
for created-entity deletions) + :class:`TemplatePropertyLookupPort` (resolves
the property name for inbound-ref restore on still-existing entities). The
ordering (rewired relationships → modified → deleted re-creates → relationship
ref re-apply → delete-created-last) is guaranteed by the builder; this use
case is a thin orchestrator.

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
from uwazi_admin_agent.domain.create_payload import strip_deleted_entity_refs
from uwazi_admin_agent.domain.file_restore import build_file_restore_actions
from uwazi_admin_agent.domain.manifest import RunStatus
from uwazi_admin_agent.domain.relationship_restore import (
    InboundRef,
    remap_metadata_refs,
)
from uwazi_admin_agent.domain.revert import (
    DeleteEntityAction,
    ReapplyRelationshipRefsAction,
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
from uwazi_admin_agent.ports.template_property_port import TemplatePropertyLookupPort


class RevertRunUseCase:
    """Revert a run: load manifest, build actions, execute restores/deletes in order."""

    def __init__(
        self,
        entity_repository: EntityRepositoryPort,
        backup_store: BackupStorePort,
        audit_log: AuditLogPort | None = None,
        file_repository: FileRepositoryPort | None = None,
        template_property_lookup: TemplatePropertyLookupPort | None = None,
    ) -> None:
        self._entity_repository: EntityRepositoryPort = entity_repository
        self._backup_store: BackupStorePort = backup_store
        self._audit_log: AuditLogPort | None = audit_log
        self._file_repository: FileRepositoryPort | None = file_repository
        self._template_property_lookup: TemplatePropertyLookupPort | None = template_property_lookup

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
            new_shared_id = await self._create_deleted_entity(action, run_id, manifest)
            await self._restore_files(action.snapshot, new_shared_id, run_id)
        elif isinstance(action, ReapplyRelationshipRefsAction):
            await self._reapply_relationship_refs(action, run_id, manifest)
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

    async def _create_deleted_entity(self, action: Any, run_id: str, manifest: Any) -> str:
        """Re-create one deleted entity via the create branch, stripping co-deleted refs.

        The snapshot raw's ``metadata`` may carry in-metadata ``relationship``-
        property refs to the re-created entity itself (self) or to a co-deleted
        entity; the create branch validates those targets exist and 400s on a
        not-yet-existing one. ``_strip_deleted_refs_for_recreate`` drops them
        (snapshot raw untouched — raw fidelity) so create succeeds; the mutual
        relationships are re-applied separately by ``_reapply_relationship_refs``
        after both endpoints exist. Records the minted sharedId on the manifest.
        """
        deleted_ids = {e.shared_id for e in manifest.deleted}
        stripped_raw = self._strip_deleted_refs_for_recreate(action.snapshot.raw, deleted_ids)
        new_shared_id = await self._entity_repository.create_raw(stripped_raw)
        self._record_restored_shared_id(manifest, action.snapshot.shared_id, new_shared_id)
        self._emit(run_id, "recreate_entity", [new_shared_id])
        logger.info(
            "revert: re-created entity (old sharedId={} -> new sharedId={})",
            action.snapshot.shared_id,
            new_shared_id,
        )
        return new_shared_id

    @staticmethod
    def _strip_deleted_refs_for_recreate(raw: dict[str, Any], deleted_ids: set[str]) -> dict[str, Any]:
        """Return a copy of ``raw`` with co-deleted/self metadata refs stripped.

        Only ``metadata`` is changed (via :func:`strip_deleted_entity_refs`);
        the rest of ``raw`` is passed through to ``create_raw``, whose
        ``to_create_payload`` keeps the allowed data fields. The snapshot's raw
        is not mutated (a shallow copy + a new metadata dict).
        """
        copy = dict(raw)
        copy["metadata"] = strip_deleted_entity_refs(raw.get("metadata", {}) or {}, deleted_ids)
        return copy

    async def _reapply_relationship_refs(self, action: Any, run_id: str, manifest: Any) -> None:
        """Re-apply relationship refs after deleted entities are re-created.

        Two scopes (both best-effort — a failure is logged + audited but does
        NOT abort the revert; entities are already re-created with their data,
        and gaps surface in post-revert verification):
        - ``recreate_targets``: for each re-created deleted entity whose snapshot
          metadata referenced a co-deleted entity, fetch its current raw (by the
          NEW sharedId), set ``metadata = remap_metadata_refs(snapshot.metadata,
          id_map)``, and ``save_raw`` it. The entity-save path's
          ``saveEntityBasedReferences`` (``updateEntities=false``) rebuilds the
          outgoing hub(s) from the remapped metadata — no self-refs.
        - ``inbound_targets``: for each still-existing entity that referenced a
          deleted entity (cascade-stripped), fetch its current raw, resolve the
          property name on its template, append the remapped ``{value: NEW,
          label}`` entry, and ``save_raw`` it. Grouped by existing entity so each
          is fetched + saved once. A missing lookup port or unresolved property
          skips that ref (logged + audited).
        """
        id_map = {e.shared_id: e.restored_shared_id for e in manifest.deleted if e.restored_shared_id}
        await self._reapply_recreated_refs(action.recreate_targets, id_map, run_id)
        await self._reapply_inbound_refs(action.inbound_targets, id_map, run_id)

    async def _reapply_recreated_refs(self, recreate_targets: list[str], id_map: dict[str, str], run_id: str) -> None:
        """Re-save each re-created deleted entity with remapped snapshot metadata."""
        for old_sid in recreate_targets:
            new_sid = id_map.get(old_sid)
            if not new_sid:
                continue
            try:
                snapshot = self._backup_store.load_snapshot(run_id, old_sid)
                current = await self._entity_repository.get_raw_by_shared_id(new_sid)
            except Exception as exc:  # noqa: BLE001 - best-effort
                self._emit_rel(run_id, failed=True, detail=f"reapply recreate {old_sid}: {exc}")
                logger.warning("revert: reapply recreate load failed old={}: {}", old_sid, exc)
                continue
            current = dict(current)
            current["metadata"] = remap_metadata_refs(
                (snapshot.raw.get("metadata") or {}) if isinstance(snapshot.raw, dict) else {},
                id_map,
            )
            try:
                await self._entity_repository.save_raw(current)
            except Exception as exc:  # noqa: BLE001 - best-effort
                self._emit_rel(run_id, failed=True, detail=f"reapply recreate {old_sid}: {exc}")
                logger.warning("revert: reapply recreate save failed new={}: {}", new_sid, exc)
                continue
            self._emit(run_id, "reapply_recreated_refs", [new_sid])
            logger.info("revert: re-applied relationship refs new sharedId={}", new_sid)

    async def _reapply_inbound_refs(self, inbound_targets: list[InboundRef], id_map: dict[str, str], run_id: str) -> None:
        """Re-add cascade-stripped inbound refs on still-existing entities."""
        if not inbound_targets:
            return
        if self._template_property_lookup is None:
            logger.warning(
                "revert: {} inbound ref(s) skipped (no template_property_lookup port wired)",
                len(inbound_targets),
            )
            self._emit_rel(run_id, failed=True, detail="no template_property_lookup port wired")
            return
        grouped: dict[str, list[InboundRef]] = {}
        for ref in inbound_targets:
            grouped.setdefault(ref.existing_shared_id, []).append(ref)
        for existing_sid, refs in grouped.items():
            try:
                current = await self._entity_repository.get_raw_by_shared_id(existing_sid)
            except Exception as exc:  # noqa: BLE001 - best-effort
                self._emit_rel(run_id, failed=True, detail=f"inbound fetch {existing_sid}: {exc}")
                logger.warning("revert: inbound fetch failed existing={}: {}", existing_sid, exc)
                continue
            current = dict(current)
            metadata = dict(current.get("metadata") or {})
            saved = False
            for ref in refs:
                new_to = id_map.get(ref.deleted_shared_id)
                if not new_to:
                    continue
                label = self._deleted_label(run_id, ref.deleted_shared_id)
                prop_name = await self._resolve_property_name(current, ref)
                if prop_name is None:
                    self._emit_rel(
                        run_id, failed=True, detail=f"inbound unresolved property {existing_sid}->{ref.deleted_shared_id}"
                    )
                    logger.warning(
                        "revert: inbound property unresolved existing={} type={} deleted={}",
                        existing_sid,
                        ref.relation_type,
                        ref.deleted_shared_id,
                    )
                    continue
                entries = list(metadata.get(prop_name) or [])
                if any(str(e.get("value")) == str(new_to) for e in entries if isinstance(e, dict)):
                    continue
                entries.append({"value": new_to, "label": label})
                metadata[prop_name] = entries
                saved = True
            if not saved:
                continue
            current["metadata"] = metadata
            try:
                await self._entity_repository.save_raw(current)
            except Exception as exc:  # noqa: BLE001 - best-effort
                self._emit_rel(run_id, failed=True, detail=f"inbound save {existing_sid}: {exc}")
                logger.warning("revert: inbound save failed existing={}: {}", existing_sid, exc)
                continue
            self._emit(run_id, "restore_inbound_relationship", [existing_sid])
            logger.info("revert: re-applied inbound refs on existing sharedId={}", existing_sid)

    def _deleted_label(self, run_id: str, deleted_sid: str) -> str:
        """Best-effort label for a re-added ref (the deleted entity's title)."""
        try:
            snapshot = self._backup_store.load_snapshot(run_id, deleted_sid)
            title = snapshot.raw.get("title") if isinstance(snapshot.raw, dict) else None
            return title if isinstance(title, str) else ""
        except Exception:
            return ""

    async def _resolve_property_name(self, existing_raw: dict[str, Any], ref: InboundRef) -> str | None:
        """Resolve the relationship property name on the existing entity's template."""
        if self._template_property_lookup is None:
            return None
        template_id = existing_raw.get("template") if isinstance(existing_raw, dict) else None
        if not isinstance(template_id, str) or not template_id:
            return None
        return await self._template_property_lookup.find_relationship_property_name(
            template_id, ref.relation_type, ref.deleted_template_id
        )

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

    def _emit_rel(self, run_id: str, *, failed: bool, detail: str | None = None) -> None:
        """Append one mutual-relationship-restore audit record (no-op if no audit log)."""
        if self._audit_log is None:
            return
        self._audit_log.append(
            run_id,
            make_audit_record(
                run_id=run_id,
                step=AuditStep.REVERT,
                op_kind="restore_relationship_failed" if failed else "restore_relationships",
                shared_ids=[],
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
