"""Revert a run by loading the manifest + snapshots and restoring exactly (§2.3, §2.6, Phase 4).

The production revert path: load the persisted manifest, call the pure
:func:`build_revert_actions` to get the ordered action list, and execute each
the :class:`EntityRepositoryPort` (``save_raw`` for entity/relationship
restores, ``create_raw`` for deleted-entity re-creates, ``delete_by_shared_id``
for created-entity deletions) + :class:`FileRepositoryPort` (deleted-file
re-uploads) + :class:`TemplatePropertyLookupPort` (resolves
the property name for inbound-ref restore on still-existing entities). The
ordering (rewired relationships → modified → deleted re-creates → relationship
ref re-apply → deleted-file re-uploads → delete-created-last) is guaranteed by the
builder; this use case is a thin orchestrator.

Phase 6 adds an **audit record** per revert action (step=REVERT) so the run's
audit log records the restore alongside the execute writes that produced it.
Optional injection keeps existing tests green; the runtime always wires a real
log.

The optional ``cache_control`` port (the runtime wires the shared cache)
makes revert cache-resilient: a re-upload mutates the FILES collection (a
raw's documents/attachments are a runtime JOIN by sharedId, not entity-row
state), so nothing on the entity-row write paths invalidates it — this use
case drops the re-upload targets' cached raws itself after the uploads
(lossless by construction: the cache is a read-through mirror and entries
repopulate lazily; ``None`` = cache disabled = no-op, the existing
convention).

Testable with an in-memory repo + in-memory backup store (the DoD's "with an
in-memory repo").
"""

import asyncio
import threading
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from uwazi_admin_agent.domain.audit_record import AuditOutcome, AuditStep, make_audit_record
from uwazi_admin_agent.domain.connection_hubs import remap_hub_rows, rows_to_delete
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
    RestoreConnectionHubsAction,
    RestoreDeletedFilesAction,
    RestoreEntityAction,
    RestorePublishStateAction,
    RestoreRelationshipAction,
    build_revert_actions,
)
from uwazi_admin_agent.domain.revert_gate import RevertRefusedError, decide_revert_gate
from uwazi_admin_agent.domain.snapshot import EntityRowSnapshot, EntitySnapshot
from uwazi_admin_agent.ports.audit_log_port import AuditLogPort
from uwazi_admin_agent.ports.backup_store_port import BackupStorePort
from uwazi_admin_agent.ports.cache_invalidation_port import CacheInvalidationPort
from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort
from uwazi_admin_agent.ports.file_repository_port import FileRepositoryPort
from uwazi_admin_agent.ports.publish_state_port import PublishStatePort
from uwazi_admin_agent.ports.relationship_hub_port import RelationshipHubPort
from uwazi_admin_agent.ports.suggestion_store_port import SuggestionStorePort
from uwazi_admin_agent.ports.template_property_port import TemplatePropertyLookupPort


def _is_not_found(exc: Exception) -> bool:
    """True when a repository error means "this row/entity does not exist".

    The adapters raise ``RuntimeError`` with Uwazi's status/body text, so the
    match is textual (the previous behavior); kept in one place so the revert
    path never again guesses at a 404 substring inline.
    """
    message = str(exc).lower()
    return "not found" in message or "404" in message


class RevertRunUseCase:
    """Revert a run: load manifest, build actions, execute restores/deletes in order."""

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

        Returns the snapshot restored (its ``captured_at`` feeds the tooltip),
        or ``None`` when the run has no snapshot for this entity.

        Flips the run's APPLIED rows of THIS sharedId to REVERTED (keep the
        table honest across languages: all the entity's rows' writes — any
        language — were undone by this single restore). Best-effort, like the
        run-level path.
        """
        manifest = self._backup_store.load_manifest(run_id)
        if shared_id in {e.shared_id for e in manifest.created}:
            raise RevertRefusedError(
                "this row's entity was created by the run — revert it from the run-level Revert (undo deletes the entity)"
            )
        if shared_id in {e.shared_id for e in manifest.deleted}:
            # A deleted entity has no row to update: its undo is a RE-CREATE
            # (fresh sharedId + files + relationship rows), which only the
            # ordered run-level revert can do. Attempting the per-entity update
            # used to 404 against the dead id, skip silently, and report
            # success while nothing was restored.
            raise RevertRefusedError(
                "this row's entity was deleted by the run — use the run-level Revert "
                "(the undo re-creates the entity; an in-place restore is impossible)"
            )
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
        if self._suggestion_store is not None:
            try:
                if hasattr(self._suggestion_store, "mark_applied_reverted_for"):
                    self._suggestion_store.mark_applied_reverted_for(run_id, [shared_id])
            except Exception as exc:  # noqa: BLE001 — status refresh must not fail the revert
                logger.warning("could not mark rows reverted for entity {}: {}", shared_id, exc)
        return snapshot

    def _mark_rows_reverted(self, run_id: str) -> int:
        """Move the run's APPLIED review rows to REVERTED (best-effort, never fails a revert).

        The writes are already undone by this point; this only keeps the review
        table honest. A store failure (or no store wired) is logged, never
        raised — the revert itself succeeded and must not be reported as failed.
        """
        if self._suggestion_store is None:
            return 0
        try:
            return self._suggestion_store.mark_applied_reverted(run_id)
        except Exception as exc:  # noqa: BLE001 — a status refresh must not fail the revert
            logger.warning("could not mark review rows reverted for run={}: {}", run_id, exc)
            return 0

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

    async def _restore_entity(self, action: Any, run_id: str) -> None:
        """Restore ONE modified entity to its first-touch snapshot — every language row.

        A Run writes per (entity, language) ROW; the same entity can carry
        writes in several languages (a translation task's ``es`` and ``en``
        rows), and a write to one row never touches the others. The snapshot
        captures EVERY configured language's row of the entity at first touch,
        so restoring ``rows`` in turn undoes the whole entity's writes. A legacy
        snapshot (no ``rows``, captured by an older build) falls back to the
        single ``raw`` — the previous behavior.

        A row that no longer exists is skipped ONLY when another row of the same
        entity still exists. "Not found" used to be swallowed unconditionally,
        which is how a delete whose revert tried to UPDATE the dead sharedId
        reported success while the entity stayed gone (the run then flipped to
        REVERTED and the revert gate refused a retry). If no row of the entity
        can be read, the restore is impossible in place: raise, so the run is
        marked FAILED — a state the gate allows reverting again.
        """
        snapshot = action.snapshot
        if snapshot.rows:
            rows = snapshot.rows
        else:
            rows = [
                EntityRowSnapshot(
                    language=snapshot.language or "",
                    internal_id=snapshot.internal_id,
                    raw=snapshot.raw,
                )
            ]
        for row in rows:
            raw = row.raw
            row_lang = row.language or snapshot.language or "en"
            try:
                await self._entity_repository.save_raw({**raw, "language": row_lang})
            except RuntimeError as exc:
                if not _is_not_found(exc):
                    raise
                if await self._entity_has_any_row(snapshot, rows):
                    logger.warning(
                        "revert: row absent sharedId={} language={} (skipped): {}",
                        snapshot.shared_id,
                        row_lang,
                        exc,
                    )
                    continue
                raise RuntimeError(
                    f"revert cannot restore sharedId={snapshot.shared_id}: the entity no longer "
                    f"exists (every language row is gone), so an in-place restore is impossible"
                ) from exc
            logger.debug("revert: restored row sharedId={} language={}", snapshot.shared_id, row_lang)

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
        existing = self._restored_shared_id_for(manifest, action.snapshot.shared_id)
        if existing is not None:
            return existing
        deleted_ids = {e.shared_id for e in manifest.deleted}
        stripped_raw = self._strip_deleted_refs_for_recreate(action.snapshot.raw, deleted_ids)
        new_shared_id = await self._entity_repository.create_raw(stripped_raw)
        self._record_restored_shared_id(manifest, action.snapshot.shared_id, new_shared_id)
        # Checkpoint the restored mapping to the manifest immediately so a
        # mid-crash revert can be resumed: on re-run, ``_create_deleted_entity``
        # short-circuits on this ``restored_shared_id`` (skipping a second
        # ``create_raw`` that would mint a duplicate/orphan entity), while the
        # relationship re-apply and file-restore steps still resolve the old→new
        # id via this mapping.
        self._backup_store.save_manifest(run_id, manifest)
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
          deleted entity (cascade-stripped), fetch its current raw **in the row
          language the ref was captured on** (``InboundRef.existing_language`` —
          a sharedId has one row per configured language), resolve the property
          name on its template, append the remapped ``{value: NEW, label}``
          entry, and ``save_raw`` it. Grouped by (existing entity, language) so
          each row is fetched + saved once. A missing lookup port or unresolved
          property skips that ref (logged + audited).

        Threading the language through both the fetch and the ``save_raw`` (whose
        ``locale`` cookie selects the written row) is what keeps the restore on
        the right locale: without it a multilingual entity's ref is re-added to
        whichever row the server returns first, and the row it was stripped from
        stays broken.
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
                current = await self._entity_repository.get_raw_by_shared_id(new_sid, self._snapshot_language(snapshot))
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
        # Grouped by (existing entity, ROW language): a sharedId has one row per
        # configured language (Uwazi multilingual entities) and the fetch + the
        # subsequent save_raw must both target the row the ref was captured on —
        # otherwise the re-added ref lands on a different locale than the one the
        # delete cascade stripped it from. The locale cookie picks the write's row.
        grouped: dict[tuple[str, str | None], list[InboundRef]] = {}
        for ref in inbound_targets:
            grouped.setdefault((ref.existing_shared_id, ref.existing_language), []).append(ref)
        for (existing_sid, language), refs in grouped.items():
            try:
                current = await self._entity_repository.get_raw_by_shared_id(existing_sid, language)
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

    async def _restore_publish_state(self, action: RestorePublishStateAction, run_id: str) -> None:
        """Restore an entity's publish + permission grants (the raw write skips both).

        A missing port is a real gap, not a success: the run's writes changed
        publishing, so the operator must see that this step did not run.
        """
        if self._publish_state is None:
            self._emit(run_id, "restore_publish_state_skipped", [action.shared_id])
            logger.warning(
                "revert: publish state restore skipped sharedId={} (no publish_state port wired)", action.shared_id
            )
            return
        await self._publish_state.restore(action.shared_id, action.state)
        self._emit(run_id, "restore_publish_state", [action.shared_id])
        logger.info(
            "revert: publish state restored sharedId={} published={} grants={}",
            action.shared_id,
            action.state.published,
            len(action.state.permissions),
        )

    async def _restore_connection_hubs(self, action: RestoreConnectionHubsAction, run_id: str, manifest: Any) -> None:
        """Replay captured connection rows and drop the ones the run added.

        Order matters inside the step: delete stale rows FIRST (a hub the run
        created must not survive alongside its restored twin), then save the
        captured set. A re-created entity's rows are remapped onto the NEW
        sharedId; they carry no live row ids (the old ones died with the old
        entity). Best-effort, like the other relationship restore paths: the
        entity content is already restored and gaps surface in verification.
        """
        if self._relationship_hubs is None:
            self._emit_rel_for_hubs(run_id, failed=True, detail=f"no relationship_hubs port wired for {action.shared_id}")
            logger.warning(
                "revert: connection hub restore skipped sharedId={} (no relationship_hubs port wired)",
                action.shared_id,
            )
            return
        id_map = {e.shared_id: e.restored_shared_id for e in manifest.deleted if e.restored_shared_id}
        target = action.target_shared_id or id_map.get(action.shared_id) or action.shared_id
        rows = remap_hub_rows(action.rows, id_map)
        for row in rows:
            if row.get("entity") == action.shared_id and target != action.shared_id:
                row["entity"] = target
        try:
            if action.current_row_ids:
                await self._relationship_hubs.delete_rows(action.current_row_ids, action.language)
            await self._relationship_hubs.restore_rows(rows, action.language)
        except Exception as exc:  # noqa: BLE001 — best-effort; verification reports the gap
            self._emit_rel_for_hubs(run_id, failed=True, detail=f"connection hubs {action.shared_id}: {exc}")
            logger.warning("revert: connection hub restore failed sharedId={}: {}", action.shared_id, exc)
            return
        self._emit(run_id, "restore_connection_hubs", [target])
        logger.info(
            "revert: connection rows restored sharedId={} rows={} deleted_stale={}",
            target,
            len(rows),
            len(action.current_row_ids),
        )

    def _load_current_hubs(self, shared_id: str, language: str | None) -> list[dict[str, Any]]:
        """Live connection rows for one entity at revert-build time ([] when unwired).

        Called synchronously while building the action list, from inside the
        revert coroutine — so the port's awaitable runs on a PRIVATE loop in a
        worker thread (``asyncio.run`` in this thread would refuse: a loop is
        already running). A failure degrades to ``[]`` — the captured rows are
        still replayed; only the "delete what the run added" half is skipped,
        and verification reports any remaining gap.
        """
        if self._relationship_hubs is None:
            return []
        port = self._relationship_hubs
        captured: dict[str, Any] = {}

        def _work() -> None:
            try:
                captured["rows"] = asyncio.run(port.list_for_entity(shared_id, language))
            except Exception as exc:  # noqa: BLE001 — best-effort read
                captured["error"] = exc

        worker = threading.Thread(target=_work, daemon=True)
        worker.start()
        worker.join()
        if "error" in captured:
            logger.warning("revert: could not read live connection rows for {}: {}", shared_id, captured["error"])
            return []
        rows = captured.get("rows") or []
        return rows if isinstance(rows, list) else []

    def _emit_rel_for_hubs(self, run_id: str, *, failed: bool, detail: str | None = None) -> None:
        """Append one connection-hub restore audit record (no-op if no audit log)."""
        if self._audit_log is None:
            return
        self._audit_log.append(
            run_id,
            make_audit_record(
                run_id=run_id,
                step=AuditStep.REVERT,
                op_kind="restore_connection_hubs_failed" if failed else "restore_connection_hubs",
                shared_ids=[],
                outcome=AuditOutcome.FAILURE if failed else AuditOutcome.SUCCESS,
                detail=detail,
            ),
        )

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

        Crash-safe/resumable: files already marked ``restored`` on the snapshot
        (checkpointed by a prior, interrupted attempt) are skipped, and each
        successful upload is checkpointed back into the snapshot immediately.
        """
        if self._file_repository is None or not snapshot.files:
            return
        old_shared_id = snapshot.shared_id
        pending = [ref for ref in snapshot.files if not ref.restored]
        actions = build_file_restore_actions(pending)
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
                self._mark_file_restored(run_id, snapshot, act.file_id)
                self._emit_file(run_id, new_shared_id, failed=False)
                logger.debug("revert: re-uploaded file sharedId={} originalname={}", new_shared_id, act.originalname)
            else:
                self._emit_file(run_id, new_shared_id, failed=True, detail=act.originalname)
                logger.warning("revert: file upload failed sharedId={} originalname={}", new_shared_id, act.originalname)
        # The re-uploads mutated the target's files collection — drop its cached
        # raw (belt-and-suspenders: the recreate's create_raw had nothing cached
        # under the fresh sharedId, but uploads alone would leave a stale entry).
        self._invalidate([new_shared_id])

    def _mark_file_restored(self, run_id: str, snapshot: Any, file_id: str) -> None:
        """Checkpoint one successfully re-uploaded file back into the snapshot.

        Marks the matching :class:`FileRef` ``restored`` and re-saves the snapshot
        so a resumed revert skips it (``_restore_files`` filters on ``restored``).
        Only the additive ``files`` metadata list is touched — the snapshot's
        ``raw`` is never mutated (raw fidelity).
        """
        files = snapshot.files or []
        updated = [ref.model_copy(update={"restored": True}) if ref.file_id == file_id else ref for ref in files]
        self._backup_store.save_snapshot(run_id, snapshot.model_copy(update={"files": updated}))

    async def _upload_one(self, action: Any, data: bytes, new_shared_id: str) -> bool:
        """Dispatch one file-restore action to the file repository."""
        assert self._file_repository is not None  # guarded by _restore_files
        language = action.language
        title = action.originalname
        content_type = action.content_type
        if action.kind == "upload_document":
            return await self._file_repository.upload_document(data, new_shared_id, language, title, content_type)
        return await self._file_repository.upload_attachment(data, new_shared_id, language, title, content_type)

    async def _restore_deleted_files(self, action: RestoreDeletedFilesAction, run_id: str, manifest: Any) -> None:
        """Re-upload every run-deleted file row (explicit + dedupe deletions).

        Target: the entity's CURRENT sharedId — or its re-created NEW sharedId
        when the same run also deleted the entity (``manifest.deleted``'
        ``restored_shared_id``, minted by the ``RecreateEntityAction`` that
        already ran — file re-uploads and entity raw restores are orthogonal,
        which is why file rows could not ride the entity snapshots in the
        first place). Best-effort per file, like ``_restore_files``: a failed
        load/upload is logged + audited as ``restore_file_failed`` but never
        aborts the revert — the gap surfaces in post-revert verification.
        Re-uploading a dedupe-source delete re-creates a duplicate copy (the
        correct undo of a dedupe cleanup); the driver's revert summary says
        so plainly via the record's ``source``.

        Crash-safe/resumable: files already marked ``restored`` on the manifest
        (checkpointed by a prior, interrupted attempt) are skipped, and each
        successful upload is checkpointed back into the manifest immediately.
        """
        if self._file_repository is None or not action.files:
            return
        id_map = {e.shared_id: e.restored_shared_id for e in manifest.deleted if e.restored_shared_id}
        targets: set[str] = set()
        for record in action.files:
            target = id_map.get(record.shared_id, record.shared_id)
            targets.add(target)
            if record.restored:
                continue
            try:
                data = self._backup_store.load_file_bytes(run_id, record.shared_id, record.file_id)
            except Exception as exc:  # noqa: BLE001 — best-effort; missing bytes must not abort revert
                self._emit_file(run_id, target, failed=True, detail=f"load bytes: {exc}")
                logger.warning(
                    "revert: deleted-file bytes missing run={} old={} fileId={}: {}",
                    run_id,
                    record.shared_id,
                    record.file_id,
                    exc,
                )
                continue
            ok = await self._upload_deleted_file(record, data, target)
            if ok:
                self._record_deleted_file_restored(manifest, record.shared_id, record.file_id)
                self._backup_store.save_manifest(run_id, manifest)
                self._emit_file(run_id, target, failed=False)
                logger.debug("revert: re-uploaded deleted file sharedId={} originalname={}", target, record.originalname)
            else:
                self._emit_file(run_id, target, failed=True, detail=record.originalname)
                logger.warning("revert: deleted-file upload failed sharedId={} originalname={}", target, record.originalname)
        # The re-uploads mutated each target's files collection (a runtime JOIN,
        # not entity-row state) — drop the targets' cached raws so the next
        # read sees the restored files. Uniform over successes and failures
        # (lossless either way); re-created entities' fresh sharedIds have
        # nothing cached, so invalidating them is a harmless no-op.
        self._invalidate(sorted(targets))

    @staticmethod
    def _record_deleted_file_restored(manifest: Any, shared_id: str, file_id: str) -> None:
        """Mark the matching run-deleted file entry ``restored`` on the manifest.

        :class:`DeletedFile` is frozen, so the entry is replaced in place with a
        copy carrying ``restored=True`` (matched by its ``(shared_id, file_id)``).
        """
        for idx, entry in enumerate(manifest.deleted_files):
            if entry.shared_id == shared_id and entry.file_id == file_id:
                manifest.deleted_files[idx] = entry.model_copy(update={"restored": True})
                return
        logger.warning("revert: no manifest.deleted_files entry for sharedId={} fileId={}", shared_id, file_id)

    async def _upload_deleted_file(self, record: Any, data: bytes, target: str) -> bool:
        """Dispatch one deleted-file re-upload to the right endpoint.

        Mirrors :meth:`_upload_one` but keys on :class:`DeletedFile`'s
        ``document``/``attachment`` literals (``_upload_one`` expects
        :class:`FileRestoreAction`'s ``upload_document``/``upload_attachment``
        literals — reusing it would silently route every document to the
        attachment endpoint).
        """
        assert self._file_repository is not None  # guarded by _restore_deleted_files
        if record.kind == "document":
            return await self._file_repository.upload_document(
                data, target, record.language, record.originalname, record.content_type
            )
        return await self._file_repository.upload_attachment(
            data, target, record.language, record.originalname, record.content_type
        )

    def _invalidate(self, shared_ids: list[str]) -> None:
        """Drop cached raws for ``shared_ids`` (no-op without a wired cache control)."""
        if self._cache_control is None or not shared_ids:
            return
        self._cache_control.invalidate_entities(shared_ids)

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
