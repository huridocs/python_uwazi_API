# mypy: disable-error-code="attr-defined"
"""Revert-run module split out of ``revert_run_use_case`` (behavior preserved)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from loguru import logger

from uwazi_admin_agent.domain.audit_record import AuditOutcome, AuditStep, make_audit_record
from uwazi_admin_agent.domain.file_dedupe import file_digest
from uwazi_admin_agent.domain.file_restore import (
    extract_file_refs,
    merge_copy_candidates,
)
from uwazi_admin_agent.domain.revert import (
    build_revert_actions,
)
from uwazi_admin_agent.domain.snapshot import EntitySnapshot, FileRef
from uwazi_admin_agent.domain.suggestion import SuggestionStatus, is_revertable_merge


class MergeMixin:
    """Revert-run merge methods (moved verbatim from the original class)."""

    def _merge_sources(self, run_id: str, shared_id: str) -> list[str]:
        """The sources this run's merge row absorbed into ``shared_id`` (empty when not a merge target).

        The target↔sources mapping lives on the review rows: every merge row of
        one target carries the same ``merge_source_ids`` (see
        :func:`~uwazi_admin_agent.domain.suggestion_scoring.merge_operations`),
        so the APPLIED rows of this entity are the record of which entities the
        merge deleted.

        A merge row that is already REVERTED is read too: its source re-create
        is checkpointed on the manifest (so re-running is safe), and the file
        cleanup may be the half an older build's partial revert missed. That is
        what lets a second click on the row finish the undo instead of
        reporting "no backup".

        Only ``update_and_delete`` is handled here. A ``create_and_delete``
        target's review row carries the ``dry-created-N`` placeholder, not the
        sharedId Uwazi minted (creates have no id until Run), and undoing a
        create is a DELETION — both are the run-level ordered revert's job.
        """
        if self._suggestion_store is None:
            return []
        try:
            rows = self._suggestion_store.applied_rows_for(run_id, [shared_id])
        except Exception as exc:  # noqa: BLE001 — no rows is not an error: the plain undo still works
            logger.warning("revert: could not read the review rows of {}: {}", shared_id, exc)
            return []
        if not any(is_revertable_merge(row) for row in rows):
            try:
                reverted_rows = self._suggestion_store.get_entity(run_id, shared_id)
            except Exception as exc:  # noqa: BLE001 — wording/finish path only; the plain undo still works
                logger.warning("revert: could not read the reverted rows of {}: {}", shared_id, exc)
                return []
            rows = [row for row in reverted_rows if row.status in {SuggestionStatus.APPLIED, SuggestionStatus.REVERTED}]
        sources: list[str] = []
        for row in rows:
            if not is_revertable_merge(row):
                continue
            for source in row.merge_source_ids:
                if source and source != shared_id and source not in sources:
                    sources.append(source)
        return sources

    def _run_merge_pairs(self, run_id: str) -> list[tuple[str, list[str]]]:
        """The run's ``(target, sources)`` merge pairs, from its APPLIED review rows.

        The manifest records the target's write and the sources' deletes, but
        not that they were ONE merge — the review row does (``update_and_delete``
        carries ``merge_source_ids``). A script-run merge whose target was never
        written has no target snapshot at all, so this row is the only record
        that the copies on the target belong to the run. Empty without a store or
        a file repository (nothing to pair/delete) or on a store failure: the
        ordinary revert still runs.
        """
        if self._suggestion_store is None or self._file_repository is None:
            return []
        try:
            rows = self._suggestion_store.applied_rows_for(run_id)
        except Exception as exc:  # noqa: BLE001 — best-effort; the entity restores still run
            logger.warning("revert: could not read the merge rows of run {}: {}", run_id, exc)
            return []
        order: list[str] = []
        sources_by_target: dict[str, list[str]] = {}
        for row in rows:
            if not is_revertable_merge(row):
                continue
            if row.shared_id not in sources_by_target:
                sources_by_target[row.shared_id] = []
                order.append(row.shared_id)
            for source in row.merge_source_ids:
                if source and source != row.shared_id and source not in sources_by_target[row.shared_id]:
                    sources_by_target[row.shared_id].append(source)
        return [(target, sources_by_target[target]) for target in order if sources_by_target[target]]

    async def _revert_merge(self, run_id: str, target: str, sources: list[str], manifest: Any) -> EntitySnapshot | None:
        """Undo ONE merge row: restore the target AND re-create the sources it absorbed.

        A merge is one operation over several entities — it writes the target
        and deletes its sources — so its undo is the run-level action list
        restricted to exactly those entities: the target's snapshot restore
        (content, publish state, connection rows), each source re-created from
        its own snapshot (new sharedId, files re-uploaded), and the relationship
        refs the delete cascade stripped, re-applied once both endpoints exist.

        The merge ALSO copied the sources' files onto the target, and that copy
        is not entity state: Uwazi joins the files collection into every raw, so
        the target's (often property-scoped) snapshot cannot drop the appended
        rows. After the restores, :meth:`_discard_merge_copies` deletes each
        copy whose bytes it can trace back to an absorbed source.
        """
        scope = {target, *sources}
        actions = build_revert_actions(
            manifest,
            lambda sid: self._backup_store.load_snapshot(run_id, sid),
            self._load_current_hubs,
            shared_ids=scope,
        )
        for action in actions:
            await self._execute_action(action, run_id, manifest)
        await self._discard_merge_copies(run_id, target, sources, manifest)
        # The re-creates checkpointed their new sharedIds as they went; persist
        # the manifest once more so the run's record matches what landed. The
        # whole merge scope (target + re-created sources) is outstanding no
        # more, so the table's counts drop all of them.
        manifest.mark_reverted([target, *sources])
        self._backup_store.save_manifest(run_id, manifest)
        logger.info("revert merge: run={} target={} sources={}", run_id, target, sources)
        self._flip_rows_reverted(run_id, [target, *sources])
        self._finalize_partial_revert(run_id, manifest)
        # The undo covered several entities, so report any snapshot it restored.
        # The target comes first (a target write is the usual case), but a
        # file-only merge writes NOTHING to its target — the review row is the
        # propertyless anchor — so the target has no backup and the undo is the
        # source re-creates alone. Returning None there would report that
        # completed undo as a refusal.
        for restored in (target, *sources):
            try:
                return self._backup_store.load_snapshot(run_id, restored)
            except FileNotFoundError:
                continue
        return None

    async def _discard_merge_copies(self, run_id: str, target: str, sources: list[str], manifest: Any) -> None:
        """Delete the files a merge copied onto ``target``, verified byte-for-byte (best-effort).

        Why this cannot ride the snapshot: a merge's file move re-uploads each
        source's document/uploaded attachment to the target, and Uwazi's
        ``documents``/``attachments`` are a join over the separate files
        collection, not entity-row fields. A raw restore (``save_raw``) cannot
        remove a row it never listed, and the property-scoped target snapshot
        used by the review-path merge does not record files at all — that is
        exactly how a revert left the append behind.

        Safety is layered so an operator's unrelated upload is never destroyed:

        1. candidates come from :func:`merge_copy_candidates` — same
           ``(kind, originalname, size)`` as an absorbed source file, not a file
           the target's own snapshot recorded, and created inside the run
           window when no before-state exists;
        2. each candidate's LIVE bytes must hash to the same sha256 as one of
           the source's backed-up bytes (missing bytes = keep, never guess);
        3. only then is ``delete_file`` called. Uwazi tears the row's derived
           thumbnail down with its document (``FilesService.delete``).

        Every failure — no file repository, a missing source snapshot/bytes, an
        unreadable target raw, a rejected delete — is logged/audited and leaves
        the file in place; the revert itself is never failed by this step.
        """
        if self._file_repository is None:
            return
        try:
            deleted_ids = {entry.shared_id for entry in manifest.deleted}
            source_snapshots = []
            for source in sources:
                if source not in deleted_ids:
                    # The merge row named a source this run did not delete: no
                    # snapshot, no authoritative before-bytes → nothing to match.
                    continue
                try:
                    source_snapshots.append(self._backup_store.load_snapshot(run_id, source))
                except FileNotFoundError:
                    continue
            if not source_snapshots:
                return

            source_files = [ref for snap in source_snapshots for ref in self._snapshot_file_refs(snap)]
            source_digests = self._source_file_digests(run_id, source_snapshots)
            if not source_digests:
                logger.warning(
                    "revert merge: run={} target={} copied files kept (no source bytes backed up)",
                    run_id,
                    target,
                )
                return

            try:
                target_snapshot: EntitySnapshot | None = self._backup_store.load_snapshot(run_id, target)
            except FileNotFoundError:
                target_snapshot = None

            try:
                live_raw = await self._entity_repository.get_raw_by_shared_id(
                    target, self._snapshot_language(target_snapshot)
                )
            except Exception as exc:  # noqa: BLE001 — best-effort cleanup; the revert already landed
                logger.warning("revert merge: could not read target {} to remove copied files: {}", target, exc)
                return

            starts = [
                value
                for value in (
                    self._epoch_ms(getattr(manifest, "created_at", None)),
                    self._epoch_ms(target_snapshot.captured_at) if target_snapshot is not None else None,
                )
                if value is not None
            ]
            ends = [value for value in (self._epoch_ms(snap.captured_at) for snap in source_snapshots) if value]
            candidates = merge_copy_candidates(
                live_raw,
                source_files=source_files,
                before_file_ids=self._snapshot_file_ids(target_snapshot),
                created_after_ms=max(starts) if starts else None,
                created_before_ms=max(ends) if ends else None,
            )
            removed = 0
            for candidate in candidates:
                try:
                    data = await self._file_repository.get_file_bytes(candidate.filename)
                except Exception as exc:  # noqa: BLE001 — keep the file; verification reports it
                    self._emit_merge_copy(run_id, target, failed=True, detail=f"fetch {candidate.originalname}: {exc}")
                    continue
                if data is None or file_digest(data) not in source_digests:
                    logger.warning(
                        "revert merge: kept target file {} ({}) — its bytes match no absorbed-source file",
                        candidate.file_id,
                        candidate.originalname,
                    )
                    continue
                if await self._file_repository.delete_file(candidate.file_id):
                    removed += 1
                    self._emit_merge_copy(run_id, target, failed=False, detail=candidate.originalname)
                    logger.info(
                        "revert merge: removed copied file sharedId={} fileId={} originalname={}",
                        target,
                        candidate.file_id,
                        candidate.originalname,
                    )
                else:
                    self._emit_merge_copy(run_id, target, failed=True, detail=candidate.originalname)
                    logger.warning(
                        "revert merge: could not remove copied file sharedId={} fileId={}", target, candidate.file_id
                    )
            if removed:
                # The file join changed; drop the target's cached raw.
                self._invalidate([target])
        except Exception as exc:  # noqa: BLE001 — cleanup must never fail the revert
            logger.warning("revert merge: copied-file cleanup failed target={}: {}", target, exc)

    def _snapshot_file_refs(self, snapshot: EntitySnapshot) -> list[FileRef]:
        """The uploaded files a snapshot recorded, falling back to its raw."""
        refs = list(snapshot.files or [])
        if refs:
            return refs
        raw = snapshot.raw if isinstance(snapshot.raw, dict) else {}
        return extract_file_refs(raw)

    @staticmethod
    def _snapshot_file_ids(snapshot: EntitySnapshot | None) -> set[str] | None:
        """The target's file ids BEFORE the run, or None when it never recorded them.

        A whole-entity snapshot carries them in ``files`` and/or in the raw's
        joined ``documents``/``attachments``; an empty set there is authoritative
        ("the entity had no files"). A property-scoped snapshot's MINIMAL raw has
        no file fields and its ``files`` is None, so it cannot say what existed
        before the merge — None, and the caller falls back to the time window.
        """
        if snapshot is None:
            return None
        ids: set[str] = {ref.file_id for ref in (snapshot.files or [])}
        raw = snapshot.raw if isinstance(snapshot.raw, dict) else {}
        for key in ("documents", "attachments"):
            entries = raw.get(key)
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                file_id = entry.get("_id")
                if isinstance(file_id, str) and file_id:
                    ids.add(file_id)
        if ids:
            return ids
        return None if snapshot.scope == "property" else set()

    def _source_file_digests(self, run_id: str, source_snapshots: list[EntitySnapshot]) -> set[str]:
        """The sha256 of every absorbed source file whose bytes the backup holds."""
        digests: set[str] = set()
        for snapshot in source_snapshots:
            for ref in self._snapshot_file_refs(snapshot):
                try:
                    data = self._backup_store.load_file_bytes(run_id, snapshot.shared_id, ref.file_id)
                except Exception as exc:  # noqa: BLE001 — an uncaptured file cannot verify a delete
                    logger.debug(
                        "revert merge: source file bytes missing run={} sharedId={} fileId={}: {}",
                        run_id,
                        snapshot.shared_id,
                        ref.file_id,
                        exc,
                    )
                    continue
                digests.add(file_digest(data))
        return digests

    @staticmethod
    def _epoch_ms(value: Any) -> int | None:
        """A datetime/Uwazi epoch value as integer epoch milliseconds (None when absent)."""
        if isinstance(value, datetime):
            return int(value.timestamp() * 1000)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        return None

    def _emit_merge_copy(self, run_id: str, target: str, *, failed: bool, detail: str | None = None) -> None:
        """Append one merge-copy cleanup audit record (no-op if no audit log)."""
        if self._audit_log is None:
            return
        self._audit_log.append(
            run_id,
            make_audit_record(
                run_id=run_id,
                step=AuditStep.REVERT,
                op_kind="delete_merge_copy_failed" if failed else "delete_merge_copy",
                shared_ids=[target],
                outcome=AuditOutcome.FAILURE if failed else AuditOutcome.SUCCESS,
                detail=detail,
            ),
        )

    def _deleted_label(self, run_id: str, deleted_sid: str) -> str:
        """Best-effort label for a re-added ref (the deleted entity's title)."""
        try:
            snapshot = self._backup_store.load_snapshot(run_id, deleted_sid)
            title = snapshot.raw.get("title") if isinstance(snapshot.raw, dict) else None
            return title if isinstance(title, str) else ""
        except Exception:
            return ""
