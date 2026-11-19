# mypy: disable-error-code="attr-defined"
"""Revert-run module split out of ``revert_run_use_case`` (behavior preserved)."""

from __future__ import annotations

from typing import Any

from loguru import logger

from uwazi_admin_agent.domain.file_dedupe import file_digest
from uwazi_admin_agent.domain.file_restore import (
    build_file_restore_actions,
    extract_file_refs,
)
from uwazi_admin_agent.domain.revert import (
    RestoreDeletedFilesAction,
)


class FilesMixin:
    """Revert-run files methods (moved verbatim from the original class)."""

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
            if await self._file_already_present(new_shared_id, act, data):
                # A resumed/older revert (or a crash between upload and checkpoint)
                # may already have put this file on the re-created entity. Re-uploading
                # would mint a duplicate, so treat it as restored.
                self._mark_file_restored(run_id, snapshot, act.file_id)
                logger.info(
                    "revert: file already present on re-created sharedId={} originalname={}",
                    new_shared_id,
                    act.originalname,
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

    async def _file_already_present(self, shared_id: str, action: Any, data: bytes) -> bool:
        """True when the re-created entity already joins this exact content.

        The revert checkpoint marks each successful upload, but a crash between
        the upload and the checkpoint (and snapshots written by older builds)
        can leave a file on the entity with ``restored=False``. Blindly
        re-uploading would duplicate it, so the live raw is checked by identity
        (kind/originalname/size) and then by sha256 — the same no-loss test the
        merge mover uses. A read/fetch failure reads as "absent" (upload).
        """
        assert self._file_repository is not None  # guarded by _restore_files
        kind = "document" if action.kind == "upload_document" else "attachment"
        try:
            raw = await self._entity_repository.get_raw_by_shared_id(shared_id, None)
        except Exception:  # noqa: BLE001 — cannot prove it is present; the upload proceeds
            return False
        digest = file_digest(data)
        for ref in extract_file_refs(raw):
            if ref.kind != kind or ref.originalname != action.originalname:
                continue
            if ref.size is not None and ref.size != len(data):
                continue
            try:
                existing = await self._file_repository.get_file_bytes(ref.filename)
            except Exception:  # noqa: BLE001 — unreadable candidate is not a confirmed duplicate
                continue
            if existing is not None and file_digest(existing) == digest:
                return True
        return False

    def _mark_file_restored(self, run_id: str, snapshot: Any, file_id: str) -> None:
        """Checkpoint one successfully re-uploaded file back into the snapshot.

        Marks the matching :class:`FileRef` ``restored`` and re-saves the snapshot
        so a resumed revert skips it (``_restore_files`` filters on ``restored``).
        Re-reads the persisted snapshot first: one revert marks SEVERAL files in
        turn, and saving the action-time copy each time would overwrite the
        previous flags (a resumed revert then re-uploads and duplicates them).
        Only the additive ``files`` metadata list is touched — the snapshot's
        ``raw`` is never mutated (raw fidelity).
        """
        latest = snapshot
        try:
            latest = self._backup_store.load_snapshot(run_id, snapshot.shared_id)
        except FileNotFoundError:
            pass
        files = latest.files or []
        updated = [ref.model_copy(update={"restored": True}) if ref.file_id == file_id else ref for ref in files]
        self._backup_store.save_snapshot(run_id, latest.model_copy(update={"files": updated}))

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
