"""Web driver module split out of the original ``run_service`` (behavior preserved)."""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from uwazi_admin_agent.domain.revert_gate import RevertRefusedError
from uwazi_admin_agent.domain.revert_verification import format_verification_result
from uwazi_admin_agent.domain.suggestion import (
    SuggestionStatus,
    is_revertable_merge,
)
from uwazi_admin_agent.drivers.runtime import build_backup_store, build_runtime
from uwazi_admin_agent.use_cases import drift_service

from .drift import _template_id_resolver
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

    A created entity is undone by DELETING it, so its row's undo needs no
    backup: the entity is removed and the returned info has no backup date.

    Returns the entity's backup info (its ``backup_created_at`` shows in the
    toast). Raises ``RevertRefusedError`` when there is no backup (nothing was
    written by this run) and the entity was not created by it.
    """
    runtime = build_runtime(user=user, password=password, url=url)
    store = build_suggestion_store(url)
    created = bool(store.created_shared_id_for(run_id, shared_id)) or shared_id in {
        entry.shared_id for entry in runtime.backup_store.load_manifest(run_id).created
    }
    deleted = shared_id in {entry.shared_id for entry in runtime.backup_store.load_manifest(run_id).deleted}
    snapshot = await runtime.revert_use_case.revert_entity(run_id, shared_id)
    if snapshot is None and not created:
        if deleted:
            raise RevertRefusedError(
                "this row's entity was deleted by the run, but its backup is missing — nothing to restore"
            )
        raise RevertRefusedError("this row has no backup — the run has not written to this entity")
    # The restore wrote Uwazi: re-capture the baseline so the next drift check
    # does not report this very revert as someone else's change.
    await _refresh_baseline_after_revert(runtime, url, run_id)
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
    row_language = await runtime.revert_use_case.revert_property(run_id, shared_id, language, property_name, suggestion_id)
    # The property write-back moved this row's editDate: keep the baseline in
    # step so the drift banner does not flag the operator's own undo.
    await _refresh_baseline_after_revert(runtime, url, run_id)
    return row_language


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

    An entity the run CREATED is undone too — its undo is a deletion of the
    minted sharedId. An entity the run DELETED is undone by RE-CREATING it from
    its backup (a fresh sharedId, its captured files and connection rows), the
    same ordered subset the run-level revert performs for that one entity. No-backup
    entities are skipped. Per-entity failures are collected: one bad restore neither
    aborts the batch nor hides the restores that landed.
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
    manifest = runtime.backup_store.load_manifest(run_id)
    created_ids = {entry.shared_id for entry in manifest.created}
    deleted_ids = {entry.shared_id for entry in manifest.deleted}
    reverted: list[str] = []
    for index, shared_id in enumerate(seen):
        minted = store.created_shared_id_for(run_id, shared_id)
        created = shared_id in created_ids or bool(minted)
        merge_rows = store.applied_rows_for(run_id, [shared_id]) or store.get_entity(run_id, shared_id)
        if any(row.op == "create_and_delete" for row in merge_rows):
            # A create-merge's undo also re-creates the sources it absorbed:
            # deleting the created target alone would lose them.
            outcome.skipped_merge.append(shared_id)
            if progress is not None:
                try:
                    progress(index + 1, len(seen))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("could not report revert progress: {}", exc)
            continue
        try:
            snapshot = await runtime.revert_use_case.revert_entity(run_id, shared_id)
        except RevertRefusedError as exc:
            # The refusals have different remedies — a create whose minted id was
            # never recorded, versus a row with nothing to restore — so they are
            # reported apart. A DELETED entity's refusal means its backup is gone.
            logger.warning("bulk revert refused run={} sharedId={}: {}", run_id, shared_id, exc)
            if shared_id in deleted_ids:
                outcome.skipped_no_backup.append(shared_id)
            else:
                outcome.skipped_created.append(shared_id)
        except Exception as exc:  # noqa: BLE001 — one bad restore must not lose the rest
            logger.warning("bulk revert failed run={} sharedId={}: {}", run_id, shared_id, exc)
            outcome.failures.append(f"{shared_id}: {exc}")
        else:
            if snapshot is None and not created:
                outcome.skipped_no_backup.append(shared_id)
            else:
                # A created entity's undo deletes it, so it reports its MINTED
                # id: that is the entity the operator selected, in the table's
                # own naming after a Run re-pointed the row.
                reverted.append(minted or shared_id)
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
    if reverted:
        # Flip only the RESTORED entities' rows: an unselected sibling entity
        # that was also written by this run stays APPLIED (still written). A
        # created entity's rows are addressed by their placeholder too, so the
        # store matches both spellings.
        store.mark_applied_reverted_for(run_id, reverted)
        # The restores wrote Uwazi (re-creates minted fresh ids): re-capture the
        # baseline, or the next check blames the operator's own revert.
        await _refresh_baseline_after_revert(runtime, url, run_id)
    return outcome


async def _refresh_baseline_after_revert(runtime: Any, url: str, run_id: str) -> None:
    """Re-capture the drift baseline after a restore (best effort, never fails the revert).

    A revert is a WRITE to Uwazi: every restored row's editDate moved, and a
    re-created (deleted-then-restored) entity carries a brand-new sharedId. Left
    alone, the very next drift check compares live Uwazi against a baseline
    recorded BEFORE the undo and paints "Out of date" — with a detail naming the
    changes this table just performed. The baseline must describe the state the
    revert left behind, so it is re-synced here, exactly like a Run does after it
    writes.

    Threaded: it is a paged ``/api/v2/search`` per language.
    """
    try:
        await asyncio.to_thread(
            drift_service.refresh_baseline_after_run,
            build_suggestion_store(url),
            runtime.drift_probe,
            _template_id_resolver(runtime),
            run_id,
        )
    except Exception as exc:  # noqa: BLE001 — the restore landed; bookkeeping must not fail it
        logger.warning("post-revert drift baseline sync failed for {}: {}", run_id, exc)


async def revert_run(url: str, run_id: str, user: str, password: str) -> None:
    """Revert a run and verify the restore.

    Raises :class:`RevertRefusedError` on refusal and
    :class:`RevertVerificationError` when the revert succeeded but the
    post-revert verification found mismatches (persisted on the manifest).
    """
    runtime = build_runtime(user=user, password=password, url=url)
    await runtime.revert_use_case.revert(run_id)
    # The restore wrote Uwazi (and re-created deleted entities under fresh ids):
    # re-capture the baseline so the next drift check compares against the state
    # this revert produced, not the pre-revert one.
    await _refresh_baseline_after_revert(runtime, url, run_id)
    result = await runtime.verify_use_case.verify(run_id)
    if not result.ok:
        detail = format_verification_result(result)
        manifest = runtime.backup_store.load_manifest(run_id)
        manifest.error = detail
        manifest.error_step = "verify"
        runtime.backup_store.save_manifest(run_id, manifest)
        raise RevertVerificationError(detail)
