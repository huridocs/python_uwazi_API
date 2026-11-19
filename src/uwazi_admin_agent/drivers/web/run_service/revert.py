"""Web driver module split out of the original ``run_service`` (behavior preserved)."""

from __future__ import annotations

from typing import Any

from loguru import logger

from uwazi_admin_agent.adapters.sqlite_suggestion_store import SqliteSuggestionStore
from uwazi_admin_agent.domain.revert_gate import RevertRefusedError
from uwazi_admin_agent.domain.revert_verification import format_verification_result
from uwazi_admin_agent.domain.suggestion import (
    SuggestionStatus,
    is_revertable_merge,
)
from uwazi_admin_agent.drivers.runtime import build_backup_store, build_runtime

from .models import BulkRevertOutcome, EntityBackupInfo, RevertVerificationError
from .wiring import build_suggestion_store


def entity_backup_info(url: str, run_id: str, shared_id: str) -> EntityBackupInfo:
    """One entity's backup metadata for a run (the per-row revert tooltip).

    Reads the snapshot file directly (no network). A legacy/missing snapshot
    returns ``backup_created_at=None``, and the UI shows the revert button
    without a tooltip date only when a backup actually exists.
    """
    store = build_backup_store(url)
    try:
        snapshot = store.load_snapshot(run_id, shared_id)
    except FileNotFoundError:
        return EntityBackupInfo(shared_id=shared_id, backup_created_at=None)
    return EntityBackupInfo(
        shared_id=shared_id,
        backup_created_at=snapshot.captured_at,
        languages=[row.language for row in snapshot.rows if row.language]
        if snapshot.rows
        else ([snapshot.language] if snapshot.language else []),
        affected_languages=list(snapshot.affected_languages),
    )


async def revert_entity_rows(url: str, run_id: str, shared_id: str, user: str, password: str) -> EntityBackupInfo:
    """Revert ONE entity of a run to its single first-touch backup (per-row action).

    The restore rolls the WHOLE entity back — every language row the snapshot
    captured — because the review table's rows are per-language but the writes
    under one sharedId share one backup. Post-verification is skipped here (it
    is the run-level revert's job); statuses flip so the entity's rows across
    ALL languages read REVERTED and become runnable again.

    Returns the entity's backup info (its ``backup_created_at`` shows in the
    toast). Raises ``RevertRefusedError`` when there is no backup (nothing was
    written by this run) or the entity was created by the run (reverting it
    means deleting it — a run-level operation).
    """
    runtime = build_runtime(user=user, password=password, url=url)
    snapshot = await runtime.revert_use_case.revert_entity(run_id, shared_id)
    if snapshot is None:
        raise RevertRefusedError("this row has no backup — the run has not written to this entity")
    return entity_backup_info(url, run_id, shared_id)


def entity_is_merge(url: str, run_id: str, shared_id: str) -> bool:
    """Whether this run's review rows of ``shared_id`` describe a merge (``update_and_delete``).

    The Revert dialog is the caller: a merge's undo is bigger than the entity's
    own backup (the sources it absorbed are re-created), so the confirmation
    has to say so before the operator clicks. Reads the review rows (no
    network); any store failure reads as "not a merge" — the dialog then shows
    the plain wording, and the undo itself still does the right thing.

    Already-REVERTED rows are read too: clicking Revert again on a merge whose
    older revert left copied files behind is the repair path, and the dialog
    must keep the merge wording for it.
    """
    try:
        store = build_suggestion_store(url)
        rows = store.applied_rows_for(run_id, [shared_id])
        if not any(is_revertable_merge(row) for row in rows):
            rows = [
                row
                for row in store.get_entity(run_id, shared_id)
                if row.status in {SuggestionStatus.APPLIED, SuggestionStatus.REVERTED}
            ]
    except Exception as exc:  # noqa: BLE001 — wording only: never block the dialog
        logger.warning("could not read the review rows of {}: {}", shared_id, exc)
        return False
    return any(is_revertable_merge(row) for row in rows)


async def revert_suboperation(
    url: str,
    run_id: str,
    shared_id: str,
    language: str | None,
    property_name: str,
    suggestion_id: int,
    user: str,
    password: str,
) -> str:
    """Undo ONE property suboperation (one property, one entity-language row).

    The fine-grained undo the suboperation rows offer: only the clicked
    property is put back to its backup value, so the operator can keep the rest
    of the entity's writes. Returns the language row written. Raises
    ``RevertRefusedError`` when the entity has no backup or was created/deleted
    by the run (those undos stay run-level).
    """
    runtime = build_runtime(user=user, password=password, url=url)
    return await runtime.revert_use_case.revert_property(run_id, shared_id, language, property_name, suggestion_id)


async def revert_entities_selection(
    url: str,
    run_id: str,
    selection: list[tuple[str, str | None]],
    user: str,
    password: str,
    progress: Any | None = None,
) -> BulkRevertOutcome:
    """Revert the CHECKED rows' entities to their single first-touch backup.

    The mirror of ``Run`` on the selection: the checked (sharedId, language)
    rows collapse to their DISTINCT entities (one backup covers every language
    row, so an entity in multiple checked languages reverts ONCE), each is
    restored through the same per-entity path the row Revert uses
    (:meth:`RevertRunUseCase.revert_entity`), and afterwards the status flip
    covers exactly those entities' rows — a language the operator did NOT check
    still reads REVERTED when its entity was restored (its write is gone too).

    Created and deleted entities are skipped (reported, not fatal): a created
    entity's undo is a deletion and a deleted one's undo is a re-create — both
    are ordered run-level operations that only the run-level Revert performs.
    No-backup entities are skipped too. Per-entity failures are collected: one
    bad restore neither aborts the batch nor hides the restores that landed.
    ``progress`` is the optional ``(done, total)`` callback the gate's toast
    polls: one tick per DISTINCT entity attempted, skipped or not, so the
    fraction always reaches its total.

    Raises nothing for empty selection — the caller guards it.
    """
    outcome = BulkRevertOutcome(run_id=run_id)
    seen: list[str] = []
    for shared_id, _language in selection:
        if shared_id and shared_id not in seen:
            seen.append(shared_id)
    if not seen:
        return outcome
    runtime = build_runtime(user=user, password=password, url=url)
    store = build_suggestion_store(url)
    reverted: list[str] = []
    for index, shared_id in enumerate(seen):
        try:
            snapshot = await runtime.revert_use_case.revert_entity(run_id, shared_id)
        except RevertRefusedError as exc:
            # The two refusals have different remedies (delete the created entity
            # vs. re-create the deleted one), so they are reported apart.
            if "deleted by the run" in str(exc):
                outcome.skipped_deleted.append(shared_id)
            else:
                outcome.skipped_created.append(shared_id)
        except Exception as exc:  # noqa: BLE001 — one bad restore must not lose the rest
            logger.warning("bulk revert failed run={} sharedId={}: {}", run_id, shared_id, exc)
            outcome.failures.append(f"{shared_id}: {exc}")
        else:
            if snapshot is None:
                outcome.skipped_no_backup.append(shared_id)
            else:
                reverted.append(shared_id)
                logger.info("bulk revert: restored run={} sharedId={}", run_id, shared_id)
        # One tick per ATTEMPTED entity — skipped and failed included, so the
        # fraction always reaches its total. Progress is display state, never
        # an error state: a broken callback cannot fail a restore.
        if progress is not None:
            try:
                progress(index + 1, len(seen))
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not report revert progress: {}", exc)
    outcome.reverted.extend(reverted)
    if reverted and hasattr(store, "mark_applied_reverted_for"):
        # Flip only the RESTORED entities' rows: an unselected sibling entity
        # that was also written by this run stays APPLIED (still written).
        assert isinstance(store, SqliteSuggestionStore)
        store.mark_applied_reverted_for(run_id, reverted)
    return outcome


async def revert_run(url: str, run_id: str, user: str, password: str) -> None:
    """Revert a run and verify the restore.

    Raises :class:`RevertRefusedError` on refusal and
    :class:`RevertVerificationError` when the revert succeeded but the
    post-revert verification found mismatches (persisted on the manifest).
    """
    runtime = build_runtime(user=user, password=password, url=url)
    await runtime.revert_use_case.revert(run_id)
    result = await runtime.verify_use_case.verify(run_id)
    if not result.ok:
        detail = format_verification_result(result)
        manifest = runtime.backup_store.load_manifest(run_id)
        manifest.error = detail
        manifest.error_step = "verify"
        runtime.backup_store.save_manifest(run_id, manifest)
        raise RevertVerificationError(detail)
