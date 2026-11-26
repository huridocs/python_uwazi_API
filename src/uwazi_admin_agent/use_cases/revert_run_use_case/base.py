# mypy: disable-error-code="attr-defined"
"""Revert-run module split out of ``revert_run_use_case`` (behavior preserved)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from loguru import logger

from uwazi_admin_agent.domain.audit_record import AuditOutcome
from uwazi_admin_agent.domain.connection_hubs import rows_to_delete
from uwazi_admin_agent.domain.manifest import RunStatus
from uwazi_admin_agent.domain.property_restore import (
    property_restore_payload,
    restore_row_language,
    row_snapshot_for_language,
    snapshot_restore_payload,
    snapshot_row_raw,
)
from uwazi_admin_agent.domain.relationship_restore import (
    InboundRef,
)
from uwazi_admin_agent.domain.revert import (
    DeleteEntityAction,
    ReapplyRelationshipRefsAction,
    RecreateEntityAction,
    RestoreConnectionHubsAction,
    RestoreDeletedFilesAction,
    RestoreEntityAction,
    RestorePublishStateAction,
    RestoreRelationshipAction,
    build_revert_actions,
)
from uwazi_admin_agent.domain.revert_gate import RevertRefusedError, decide_revert_gate
from uwazi_admin_agent.domain.review_refresh import restored_value
from uwazi_admin_agent.domain.snapshot import EntityRowSnapshot, EntitySnapshot
from uwazi_admin_agent.domain.suggestion import TITLE_PROPERTY, SuggestionStatus
from uwazi_admin_agent.domain.suggestion_scoring import compare_values
from uwazi_admin_agent.ports.audit_log_port import AuditLogPort
from uwazi_admin_agent.ports.backup_store_port import BackupStorePort
from uwazi_admin_agent.ports.cache_invalidation_port import CacheInvalidationPort
from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort
from uwazi_admin_agent.ports.file_repository_port import FileRepositoryPort
from uwazi_admin_agent.ports.publish_state_port import PublishStatePort
from uwazi_admin_agent.ports.relationship_hub_port import RelationshipHubPort
from uwazi_admin_agent.ports.suggestion_store_port import SuggestionStorePort
from uwazi_admin_agent.ports.template_property_port import TemplatePropertyLookupPort


class BaseMixin:
    """Revert-run base methods (moved verbatim from the original class)."""

    def __init__(
        self,
        entity_repository: EntityRepositoryPort,
        backup_store: BackupStorePort,
        audit_log: AuditLogPort | None = None,
        file_repository: FileRepositoryPort | None = None,
        template_property_lookup: TemplatePropertyLookupPort | None = None,
        cache_control: CacheInvalidationPort | None = None,
        suggestion_store: SuggestionStorePort | None = None,
        publish_state: PublishStatePort | None = None,
        relationship_hubs: RelationshipHubPort | None = None,
    ) -> None:
        self._entity_repository: EntityRepositoryPort = entity_repository
        self._backup_store: BackupStorePort = backup_store
        self._audit_log: AuditLogPort | None = audit_log
        self._file_repository: FileRepositoryPort | None = file_repository
        self._template_property_lookup: TemplatePropertyLookupPort | None = template_property_lookup
        self._cache_control: CacheInvalidationPort | None = cache_control
        # Optional: the review rows' status follows the revert so the review
        # table shows them runnable again (None = no store wired = no-op).
        self._suggestion_store: SuggestionStorePort | None = suggestion_store
        # Publish/permission restore (None = unwired: snapshots without a
        # publish_state carry nothing to restore, so the step is a no-op).
        self._publish_state: PublishStatePort | None = publish_state
        # Non-property connection rows (file-cited / text citations): captured on
        # the snapshot, replayed through the bulk endpoint (None = unwired: the
        # action is skipped and reported).
        self._relationship_hubs: RelationshipHubPort | None = relationship_hubs

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

        actions = build_revert_actions(
            manifest,
            lambda sid: self._backup_store.load_snapshot(run_id, sid),
            self._load_current_hubs,
        )

        try:
            for action in actions:
                await self._execute_action(action, run_id, manifest)
        except Exception as exc:
            op_kind = type(action).__name__
            manifest.error = f"revert failed at {op_kind}: {exc}"
            manifest.error_step = "revert"
            manifest.last_executed_at = datetime.now(timezone.utc)
            self._backup_store.save_manifest(run_id, manifest)
            self._backup_store.update_status(run_id, RunStatus.FAILED)
            self._emit_run(AuditOutcome.FAILURE, run_id)
            logger.error("revert failed run={} at={} error={}", run_id, op_kind, exc)
            raise

        # A merge's file move is NOT entity state: it re-uploads the sources'
        # files onto the target, and the restore of a (possibly property-scoped)
        # target snapshot cannot drop those file rows. Remove the copies now,
        # after the ordered restores/re-creates, so their sources' bytes are
        # still available to verify each copy before deleting it. Best-effort:
        # a cleanup failure leaves the copy visible, never fails the revert.
        for merge_target, merge_sources in self._run_merge_pairs(run_id):
            await self._discard_merge_copies(run_id, merge_target, merge_sources, manifest)

        # Persist any restored_shared_id mappings recorded during re-creates so
        manifest.last_executed_at = datetime.now(timezone.utc)
        manifest.error = None
        manifest.error_step = None
        self._backup_store.save_manifest(run_id, manifest)
        self._backup_store.update_status(run_id, RunStatus.REVERTED)
        self._emit_run(AuditOutcome.SUCCESS, run_id)
        # The review rows now describe writes that no longer exist: mark them
        # reverted so the review table shows them as runnable again.
        reverted_rows = self._mark_rows_reverted(run_id)
        logger.info(
            "revert done run={} actions={} modified={} deleted={} created={} deleted_files={} review_rows={}",
            run_id,
            len(actions),
            len(manifest.modified),
            len(manifest.deleted),
            len(manifest.created),
            len(manifest.deleted_files),
            reverted_rows,
        )

    async def revert_entity(self, run_id: str, shared_id: str) -> EntitySnapshot | None:
        """Revert ONE entity of a run to its single first-touch snapshot (per-row revert).

        The review table is one row per (entity, LANGUAGE): the same entity can
        appear in several rows, each of which a separate Run may have written.
        The rows share ONE snapshot per entity (first-touch — no backup
        versions), and this restore rolls the ENTIRE entity back to it: every
        language ROW the snapshot captured, in one pass — not just the row the
        button sat on. Only snapshot-carrying entities (modified or deleted by
        the run) are revertable; created entities revert by deletion and are
        refused here.

        A MERGE row (``update_and_delete``) is the one multi-entity case: it
        writes its target AND deletes the sources it absorbed, so its undo
        restores the target and re-creates those sources (see
        :meth:`_revert_merge`). Restoring the target alone would leave the
        absorbed entities deleted.

        An entity the run CREATED is undone from its row too: there is no
        snapshot to restore, so the undo is a DELETION of the minted sharedId
        (the row's ``created_shared_id``), after which its rows read REVERTED and
        become runnable again.

        Returns the snapshot restored (its ``captured_at`` feeds the tooltip),
        ``None`` when the run has no snapshot for this entity, or an empty
        snapshot for a created entity that was deleted (nothing to restore).

        Flips the run's APPLIED rows of THIS sharedId to REVERTED (keep the
        table honest across languages: all the entity's rows' writes — any
        language — were undone by this single restore). Best-effort, like the
        run-level path.
        """
        manifest = self._backup_store.load_manifest(run_id)
        merge_sources = self._merge_sources(run_id, shared_id)
        if merge_sources:
            return await self._revert_merge(run_id, shared_id, merge_sources, manifest)
        if self._is_create_merge(run_id, shared_id):
            # A create-merge is NOT a plain create: its undo also re-creates the
            # sources it absorbed, which only the ordered run-level revert can
            # do. Deleting just the created target would lose those entities.
            raise RevertRefusedError(
                "this row is a create-merge: its undo also re-creates the sources it absorbed — use the run-level Revert"
            )
        if shared_id in {entry.shared_id for entry in manifest.created} or self._created_id_of(run_id, shared_id):
            await self._revert_created(run_id, shared_id, manifest)
            return None
        if shared_id in {e.shared_id for e in manifest.deleted}:
            # A deleted entity has no live row to update: its undo is a RE-CREATE
            # from the first-touch snapshot (fresh sharedId, files re-uploaded,
            # connection rows replayed, inbound relationship refs re-applied).
            # That is the SAME ordered action list the run-level revert builds,
            # narrowed to this one entity, so a delete row is revertable from the
            # table exactly like a modified one.
            return await self._revert_deleted(run_id, shared_id, manifest)
        try:
            snapshot = self._backup_store.load_snapshot(run_id, shared_id)
        except FileNotFoundError:
            return None
        action = RestoreEntityAction(snapshot=snapshot)
        await self._execute_action(action, run_id, manifest)
        # An in-place restore must undo EVERYTHING the run changed on this entity
        # — not just its content. Publishing and non-property connection rows are
        # written outside the entity payload, so they need their own calls here
        # exactly as they do on the run-level path.
        if snapshot.publish_state is not None:
            await self._restore_publish_state(
                RestorePublishStateAction(shared_id=shared_id, state=snapshot.publish_state), run_id
            )
        if snapshot.connection_hubs:
            current_rows = self._load_current_hubs(shared_id, snapshot.language)
            await self._restore_connection_hubs(
                RestoreConnectionHubsAction(
                    shared_id=shared_id,
                    language=snapshot.language,
                    rows=list(snapshot.connection_hubs),
                    current_row_ids=rows_to_delete(current_rows, snapshot.connection_hubs),
                ),
                run_id,
                manifest,
            )
        self._emit(run_id, "restore_entity", [shared_id])
        logger.info("revert entity: run={} sharedId={} languages={}", run_id, shared_id, [r.language for r in snapshot.rows])
        manifest.mark_reverted([shared_id])
        self._backup_store.save_manifest(run_id, manifest)
        self._flip_rows_reverted(run_id, [shared_id])
        self._finalize_partial_revert(run_id, manifest)
        return snapshot

    async def _revert_deleted(self, run_id: str, shared_id: str, manifest: Any) -> EntitySnapshot | None:
        """Undo ONE delete row: re-create the entity from its first-touch snapshot.

        The run-level revert does this for every deleted entity; a delete ROW's
        own Revert is the same ordered subset, built by
        :func:`~uwazi_admin_agent.domain.revert.build_revert_actions` with
        ``shared_ids={shared_id}`` so it also re-applies the relationship refs
        the delete cascade stripped and re-uploads the entity's captured files.

        The re-create mints a FRESH sharedId (Uwazi cannot resurrect the old
        one), checkpointed on the manifest so a resumed revert does not mint a
        second copy. The recorded row keeps the OLD id, which is why a reverted
        delete cannot be run again (``rerun_blocked``); Sync with Uwazi
        re-reads the operation from live state.

        Returns the restored backup (its ``captured_at`` feeds the toast), or
        ``None`` when the run holds no snapshot for the entity.
        """
        try:
            snapshot = self._backup_store.load_snapshot(run_id, shared_id)
        except FileNotFoundError as exc:
            raise RevertRefusedError(
                "this row's entity was deleted by the run, but its backup is missing — nothing to restore"
            ) from exc
        actions = build_revert_actions(
            manifest,
            lambda sid: self._backup_store.load_snapshot(run_id, sid),
            self._load_current_hubs,
            shared_ids={shared_id},
        )
        for action in actions:
            await self._execute_action(action, run_id, manifest)
        # The action list already restored the entity's content, files and
        # connection rows; the manifest's per-entity bookkeeping is what the
        # table's counts and a later run-level revert read.
        restored_id = self._restored_shared_id_for(manifest, shared_id) or shared_id
        manifest.mark_reverted([shared_id])
        self._backup_store.save_manifest(run_id, manifest)
        logger.info(
            "revert deleted entity: run={} old sharedId={} new sharedId={}",
            run_id,
            shared_id,
            restored_id,
        )
        self._flip_rows_reverted(run_id, [shared_id, restored_id])
        self._finalize_partial_revert(run_id, manifest)
        return snapshot

    def _is_create_merge(self, run_id: str, shared_id: str) -> bool:
        """Whether this entity's review rows describe a ``create_and_delete`` merge (pure-ish).

        Such a row's sharedId is the ``dry-created-N`` placeholder, and its undo
        must re-create the absorbed sources — so it is run-level, not a delete.
        """
        if self._suggestion_store is None:
            return False
        try:
            rows = self._suggestion_store.applied_rows_for(run_id, [shared_id])
            if not any(row.op == "create_and_delete" for row in rows):
                # A merge whose revert already ran no longer has APPLIED rows;
                # the stored rows still say what the operation was.
                rows = self._suggestion_store.get_entity(run_id, shared_id)
        except Exception as exc:  # noqa: BLE001 — a read failure degrades to "not a merge"
            logger.warning("revert: could not read the merge rows of {}: {}", shared_id, exc)
            return False
        return any(row.op == "create_and_delete" for row in rows)

    def _created_id_of(self, run_id: str, shared_id: str) -> str | None:
        """The minted sharedId behind a create row (None without a store/record)."""
        if self._suggestion_store is None:
            return None
        try:
            return self._suggestion_store.created_shared_id_for(run_id, shared_id)
        except Exception as exc:  # noqa: BLE001 — a lookup failure degrades to "not a create"
            logger.warning("revert: could not resolve the created id of {}: {}", shared_id, exc)
            return None

    async def _revert_created(self, run_id: str, shared_id: str, manifest: Any) -> None:
        """Undo a CREATE row: delete the entity the run minted, then flip its rows.

        A created entity has no before-state to restore, so its undo is a
        deletion. The id to delete is the real sharedId — the manifest's entry
        (a row addressed by its real id) or the row's ``created_shared_id`` (a
        row addressed by its ``dry-created-N`` placeholder). After the delete the
        rows read REVERTED (runnable again) and the minted id is cleared so the
        table stops linking to a page that no longer exists. A created entity
        with NO minted id (its create never ran) is refused: nothing exists to
        delete.
        """
        manifest_ids = {entry.shared_id for entry in manifest.created}
        minted = shared_id if shared_id in manifest_ids else self._created_id_of(run_id, shared_id)
        if not minted:
            raise RevertRefusedError(
                "this row's entity was created by the run but no sharedId was recorded for it, so there is nothing to delete"
            )
        await self._entity_repository.delete_by_shared_id(minted)
        self._emit(run_id, "delete_created", [minted])
        logger.info("revert created entity: run={} sharedId={}", run_id, minted)
        manifest.mark_reverted([minted])
        self._backup_store.save_manifest(run_id, manifest)
        if self._suggestion_store is not None:
            try:
                self._suggestion_store.mark_applied_reverted_for(run_id, [minted, shared_id])
                self._suggestion_store.clear_created_ids(run_id, [minted, shared_id])
            except Exception as exc:  # noqa: BLE001 — status refresh must not fail the revert
                logger.warning("could not mark created rows reverted for {}: {}", minted, exc)
        self._finalize_partial_revert(run_id, manifest)

    async def revert_property(
        self,
        run_id: str,
        shared_id: str,
        language: str | None,
        property_name: str,
        suggestion_id: int | None = None,
    ) -> str:
        """Undo ONE property write on ONE entity-language row (the suboperation undo).

        The entity-wide :meth:`revert_entity` is the coarse undo; this is the
        fine one a SUBOPERATION offers. It re-reads the live row, puts back the
        single key the snapshot captured (``_title`` for the title), and writes
        the row — leaving every other property (and every other language) the
        run wrote untouched. That is why it cannot reuse the snapshot restore:
        re-writing the whole raw would silently undo siblings the operator kept.

        Refused for entities the run created (undo = delete) or deleted (undo =
        re-create), exactly like the per-entity path. Only property rows are
        offered by the UI; a whole-entity op has no single key to put back.

        ``language=None`` (a title, a select, a date — a row with no language of
        its own) targets the row the Run actually wrote: the backup's primary
        language (see :func:`~uwazi_admin_agent.domain.property_restore.restore_row_language`).

        Returns the language row written. When ``suggestion_id`` is given, that
        one row is flipped to REVERTED (the others keep their status: their
        writes are still in Uwazi).
        """
        manifest = self._backup_store.load_manifest(run_id)
        if shared_id in {e.shared_id for e in manifest.created}:
            raise RevertRefusedError(
                "this suboperation's entity was created by the run — undo the creation from the run-level Revert"
            )
        if shared_id in {e.shared_id for e in manifest.deleted}:
            raise RevertRefusedError(
                "this suboperation's entity was deleted by the run — use the run-level Revert to re-create it"
            )
        try:
            snapshot = self._backup_store.load_snapshot(run_id, shared_id)
        except FileNotFoundError as exc:
            raise RevertRefusedError("this entity has no backup — the run has not written to it") from exc
        # A language-independent suboperation (a title, a select, a date) has no
        # language of its own; the Run wrote it to the default row, and that is
        # the row the backup captured. Resolve it BEFORE the lookup, or the
        # backup looks for a language-less row Uwazi never stores and refuses
        # the undo outright.
        row_language = restore_row_language(snapshot, language)
        snapshot_raw = snapshot_row_raw(snapshot, row_language)
        if snapshot_raw is None:
            raise RevertRefusedError(
                f"the backup has no {row_language} row for this entity, so there is nothing to restore in this language"
            )
        self._require_property_covered(snapshot, row_language, property_name)
        try:
            live_raw = await self._entity_repository.get_raw_by_shared_id(shared_id, row_language)
        except RuntimeError as exc:
            raise RuntimeError(f"cannot undo {property_name} for {shared_id} ({row_language}): {exc}") from exc
        payload = property_restore_payload(live_raw, snapshot_raw, property_name)
        await self._entity_repository.save_raw({**payload, "language": row_language})
        self._emit(run_id, "restore_property", [shared_id])
        logger.info(
            "revert property: run={} sharedId={} language={} property={}",
            run_id,
            shared_id,
            row_language,
            property_name,
        )
        if suggestion_id is not None and self._suggestion_store is not None:
            try:
                row = self._suggestion_store.get_suggestion(suggestion_id)
                restore_language = row.language if row is not None else language
                current = restored_value(snapshot_raw, property_name, restore_language)
                self._suggestion_store.set_status([suggestion_id], SuggestionStatus.REVERTED)
                self._suggestion_store.set_current_values(
                    [(suggestion_id, current, None if row is None else compare_values(row.proposed_value, current))]
                )
            except Exception as exc:  # noqa: BLE001 — status refresh must not fail the revert
                logger.warning("could not mark suboperation {} reverted: {}", suggestion_id, exc)
        # The entity is outstanding again only while another of its writes is
        # still APPLIED. A single-property undo that leaves siblings applied
        # must keep the entity in the table's Modified count.
        if self._suggestion_store is not None:
            try:
                remaining = self._suggestion_store.applied_rows_for(run_id, [shared_id])
            except Exception as exc:  # noqa: BLE001 — the restore landed; counts stay best-effort
                logger.warning("could not read the remaining applied rows of {}: {}", shared_id, exc)
            else:
                if not remaining:
                    manifest.mark_reverted([shared_id])
                    self._backup_store.save_manifest(run_id, manifest)
        self._finalize_partial_revert(run_id, manifest)
        return row_language

    async def _execute_action(self, action: Any, run_id: str, manifest: Any) -> None:
        """Dispatch one revert action to the entity repository (+ audit + manifest record)."""
        if isinstance(action, RestoreRelationshipAction):
            await self._restore_relationship(action, run_id)
        elif isinstance(action, RestoreEntityAction):
            await self._restore_entity(action, run_id)
            self._emit(run_id, "restore_entity", [action.snapshot.shared_id])
            logger.debug("revert: restored entity sharedId={}", action.snapshot.shared_id)
        elif isinstance(action, RecreateEntityAction):
            new_shared_id = await self._create_deleted_entity(action, run_id, manifest)
            await self._restore_files(action.snapshot, new_shared_id, run_id)
        elif isinstance(action, RestoreDeletedFilesAction):
            await self._restore_deleted_files(action, run_id, manifest)
        elif isinstance(action, ReapplyRelationshipRefsAction):
            await self._reapply_relationship_refs(action, run_id, manifest)
        elif isinstance(action, RestorePublishStateAction):
            await self._restore_publish_state(action, run_id)
        elif isinstance(action, RestoreConnectionHubsAction):
            await self._restore_connection_hubs(action, run_id, manifest)
        elif isinstance(action, DeleteEntityAction):
            await self._entity_repository.delete_by_shared_id(action.shared_id)
            self._emit(run_id, "delete_created", [action.shared_id])
            logger.debug("revert: deleted created entity sharedId={}", action.shared_id)

    def _finalize_partial_revert(self, run_id: str, manifest: Any) -> None:
        """Flip a partially reverted run to REVERTED once no APPLIED row remains.

        A per-entity (or per-property) revert can undo the last outstanding write
        of a run. The manifest status is what the runs table's badge and counts
        read, so leaving it EXECUTED made a fully undone run still look executed.
        When the suggestion store is unwired (a script-only run) or unreadable,
        the status is left alone — there is no row state to prove the run empty.
        """
        if self._suggestion_store is None:
            return
        try:
            remaining = self._suggestion_store.applied_rows_for(run_id)
        except Exception as exc:  # noqa: BLE001 — a store read must not fail the restore
            logger.warning("could not read the remaining applied rows of run {}: {}", run_id, exc)
            return
        if remaining:
            return
        manifest.status = RunStatus.REVERTED
        manifest.error = None
        manifest.error_step = None
        self._backup_store.save_manifest(run_id, manifest)
        self._emit_run(AuditOutcome.SUCCESS, run_id)
        logger.info("partial revert completed run={} — no applied rows remain", run_id)

    def _mark_rows_reverted(self, run_id: str) -> int:
        """Move the run's APPLIED review rows to REVERTED (best-effort, never fails a revert).

        The writes are already undone by this point; this only keeps the review
        table honest. A store failure (or no store wired) is logged, never
        raised — the revert itself succeeded and must not be reported as failed.
        """
        if self._suggestion_store is None:
            return 0
        try:
            rows = self._suggestion_store.applied_rows_for(run_id)
            updates = self._restored_current_updates(rows, run_id)
            count = self._suggestion_store.mark_applied_reverted(run_id)
            self._suggestion_store.set_current_values(updates)
            return count
        except Exception as exc:  # noqa: BLE001 — a status refresh must not fail the revert
            logger.warning("could not mark review rows reverted for run={}: {}", run_id, exc)
            return 0

    def _flip_rows_reverted(self, run_id: str, shared_ids: list[str]) -> None:
        """Mark the run's APPLIED review rows of ``shared_ids`` REVERTED (best-effort).

        The writes are already undone by this point; this only keeps the review
        table honest — every language row of every entity the undo covered. A
        store failure (or no store wired) is logged, never raised.
        """
        if self._suggestion_store is None:
            return
        try:
            applied = self._suggestion_store.applied_rows_for(run_id, shared_ids)
            updates = self._restored_current_updates(applied, run_id)
            self._suggestion_store.mark_applied_reverted_for(run_id, shared_ids)
            self._suggestion_store.set_current_values(updates)
        except Exception as exc:  # noqa: BLE001 — status refresh must not fail the revert
            logger.warning("could not mark rows reverted for {}: {}", shared_ids, exc)

    def _restored_current_updates(
        self,
        rows: list[Any],
        run_id: str,
    ) -> list[tuple[int, Any, bool | None]]:
        """``(row id, restored value, match)`` for the applied rows being undone.

        The snapshot's per-language value is the state Uwazi has after a
        revert, so it replaces the stale prediction-time ``current_value``.
        Whole-entity rows (no property) and rows without a snapshot are left
        alone.
        """
        snapshots: dict[str, EntitySnapshot | None] = {}
        updates: list[tuple[int, Any, bool | None]] = []
        for row in rows:
            if row.id is None or row.property_name is None:
                continue
            if row.shared_id not in snapshots:
                try:
                    snapshots[row.shared_id] = self._backup_store.load_snapshot(run_id, row.shared_id)
                except FileNotFoundError:
                    snapshots[row.shared_id] = None
            snapshot = snapshots[row.shared_id]
            if snapshot is None:
                continue
            row_language = restore_row_language(snapshot, row.language)
            snapshot_raw = snapshot_row_raw(snapshot, row_language)
            if snapshot_raw is None:
                continue
            current = restored_value(snapshot_raw, row.property_name, row_language)
            updates.append((row.id, current, compare_values(row.proposed_value, current)))
        return updates

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

    @staticmethod
    def _restored_shared_id_for(manifest: Any, old_shared_id: str) -> str | None:
        """Return the checkpointed new sharedId for ``old_shared_id``, or None.

        A previous (possibly interrupted) revert already re-created this deleted
        entity and checkpointed its minted ``restored_shared_id``. Resuming skips
        the ``create_raw`` (which would mint a *second* fresh sharedId and leak a
        duplicate/orphan) and reuses the recorded id for file/relationship restore.
        """
        for entry in manifest.deleted:
            if entry.shared_id == old_shared_id and entry.restored_shared_id:
                return entry.restored_shared_id
        return None

    @staticmethod
    def _snapshot_language(snapshot: Any) -> str | None:
        """The row language a snapshot was captured on (falls back to the raw's own).

        A sharedId can have one row per configured language (Uwazi multilingual
        entities). Every read/save in the restore path must address the SAME row
        the snapshot came from, so the locale is threaded through instead of being
        left to the server's default.
        """
        language = getattr(snapshot, "language", None)
        if isinstance(language, str) and language:
            return language
        raw = getattr(snapshot, "raw", None)
        raw_language = raw.get("language") if isinstance(raw, dict) else None
        return raw_language if isinstance(raw_language, str) and raw_language else None

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

    async def _entity_has_any_row(self, snapshot: EntitySnapshot, rows: list[EntityRowSnapshot]) -> bool:
        """True when at least one language row of ``snapshot``'s entity is readable.

        Distinguishes "this locale's row was never materialized" (a legitimate
        skip: nothing under that language to restore) from "the whole entity is
        gone" (fatal — the raw update branch cannot bring it back).
        """
        for candidate in {row.language or snapshot.language or "en" for row in rows} | {"en"}:
            try:
                await self._entity_repository.get_raw_by_shared_id(snapshot.shared_id, candidate)
            except Exception:  # noqa: BLE001 — a failed probe is simply not evidence of existence
                continue
            return True
        return False

    def _require_property_covered(self, snapshot: EntitySnapshot, language: str | None, property_name: str) -> None:
        """Refuse a suboperation undo the property-scoped snapshot does not cover.

        A property-scoped snapshot captures ONLY the keys the run wrote, and the
        single-property undo treats "absent from the backup" as "did not exist
        before" — i.e. it REMOVES the key. That is right for a key the run
        wrote and wrong for every other key, so a row the snapshot does not
        cover must be refused instead of silently deleting a live value.
        """
        if snapshot.scope != "property":
            return
        row = row_snapshot_for_language(snapshot, language)
        if row is None:
            return
        if property_name == TITLE_PROPERTY or property_name in row.touched_properties:
            return
        raise RevertRefusedError(
            f"this backup covers only the properties the run wrote; {property_name!r} was not one of them "
            "(revert the entity instead)"
        )

    async def _restore_property_rows(self, snapshot: EntitySnapshot, rows: list[EntityRowSnapshot]) -> None:
        """Put back only the properties a property-scoped snapshot captured.

        One ``POST /api/entities`` per captured row, carrying the exact key set
        the run wrote (see :func:`snapshot_restore_payload`). No explicit-clears
        pass, no index wait — the payload IS the restore set, so every property
        the run never touched is left exactly as it is. The repository reads the
        live row to echo its ``documents``/``attachments`` back (Uwazi deletes
        every file the update body does not re-list), which keeps the restore
        surgical AND non-destructive.

        A row whose entity is gone is fatal, like the whole-row path: a
        property restore cannot resurrect a deleted entity, and reporting
        success over one is how a run flipped to REVERTED while the entity
        stayed gone.
        """
        for row in rows:
            payload = snapshot_restore_payload(snapshot.shared_id, row)
            try:
                await self._entity_repository.save_raw_properties(payload)
            except NotImplementedError as exc:
                # There is no safe fallback: the whole-row save clears every
                # property the payload omits, and this payload omits every
                # property the run never touched. Refusing is the only honest
                # outcome.
                raise RuntimeError(
                    f"revert cannot restore the property-scoped snapshot of {snapshot.shared_id!r}: "
                    "the wired entity repository does not support property-scoped saves"
                ) from exc
            logger.debug(
                "revert: restored {} propert(ies) sharedId={} language={}",
                len(row.touched_properties),
                snapshot.shared_id,
                row.language,
            )
