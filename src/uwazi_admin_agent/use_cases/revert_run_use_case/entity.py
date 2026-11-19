# mypy: disable-error-code="attr-defined"
"""Revert-run module split out of ``revert_run_use_case`` (behavior preserved)."""

from __future__ import annotations

import asyncio
import threading
from typing import Any

from loguru import logger

from uwazi_admin_agent.domain.audit_record import AuditOutcome, AuditStep, make_audit_record
from uwazi_admin_agent.domain.connection_hubs import remap_hub_rows
from uwazi_admin_agent.domain.create_payload import strip_deleted_entity_refs
from uwazi_admin_agent.domain.relationship_restore import (
    InboundRef,
    remap_metadata_refs,
)
from uwazi_admin_agent.domain.revert import (
    RestoreConnectionHubsAction,
    RestorePublishStateAction,
    RestoreRelationshipAction,
)
from uwazi_admin_agent.domain.snapshot import EntityRowSnapshot

from .helpers import _is_not_found


class EntityMixin:
    """Revert-run entity methods (moved verbatim from the original class)."""

    async def _restore_entity(self, action: Any, run_id: str) -> None:
        """Restore ONE modified entity to its first-touch snapshot — every language row.

        A Run writes per (entity, language) ROW; the same entity can carry
        writes in several languages (a translation task's ``es`` and ``en``
        rows), and a write to one row never touches the others. The snapshot
        captures EVERY configured language's row of the entity at first touch,
        so restoring ``rows`` in turn undoes the whole entity's writes. A legacy
        snapshot (no ``rows``, captured by an older build) falls back to the
        single ``raw`` — the previous behavior.

        A **property-scoped** snapshot takes a different path on purpose: its
        rows are MINIMAL raws (identity + title + the touched keys only), so
        writing them through the whole-row save would clear every property the
        minimal raw omits. Each row is instead restored with
        :meth:`EntityRepositoryPort.save_raw_properties`, which posts exactly the
        captured key set (``[]`` for a property the run created) and leaves the
        rest of the entity alone. That is both surgical and one request per row.

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
        if snapshot.scope == "property":
            await self._restore_property_rows(snapshot, rows)
            return
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

    async def _restore_relationship(self, action: RestoreRelationshipAction, run_id: str) -> None:
        """Fetch the entity's current raw, patch the relationship field, save."""
        raw = await self._entity_repository.get_raw_by_shared_id(action.entity.shared_id, action.entity.language)
        raw[action.property_name] = action.before
        await self._entity_repository.save_raw(raw)
        self._emit(run_id, "restore_relationship", [action.entity.shared_id])
        logger.debug("revert: restored relationship sharedId={} property={}", action.entity.shared_id, action.property_name)
