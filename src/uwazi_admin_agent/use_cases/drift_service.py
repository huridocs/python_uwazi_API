"""Drift detection use case: baseline capture, cheap probe, full scan.

The review table is a snapshot of what Uwazi looked like when the planning pass
ran. A human can edit the instance afterwards, so every Run is preceded by a
drift check: the same entities are compared against the recorded ``editDate``
baseline, and a changed/new/missing entity is surfaced before a write.

This module owns the decisions; the HTTP/disk details stay in the adapters:

- :class:`~uwazi_admin_agent.ports.drift_probe_port.DriftProbePort` answers the
  cheap probe, the full scan and the entity count;
- :class:`~uwazi_admin_agent.ports.drift_store_port.DriftStorePort` persists the
  baseline and the last verdict;
- :class:`~uwazi_admin_agent.ports.backup_store_port.BackupStorePort` supplies
  the run's own written ids (they are never drift);
- the entity repository + cache supply the targeted file-set drift, because
  ``editDate`` cannot see a supporting file that was attached afterwards.

Nothing here knows about the web UI, and nothing here builds a runtime. The
driver passes the ports in, which keeps the use case testable without a live
instance.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from uwazi_admin_agent.configuration import DUMMY_LANGUAGE
from uwazi_admin_agent.domain.drift import (
    MISSING_LABEL,
    MODIFIED_LABEL,
    NEW_LABEL,
    DriftBaselineRow,
    DriftEntry,
    DriftKind,
    DriftReport,
    DriftStateRow,
    DriftStatus,
    build_probe_result,
    build_report,
)
from uwazi_admin_agent.domain.file_drift import changed_file_entities, recorded_file_languages, recorded_file_signatures
from uwazi_admin_agent.domain.file_restore import extract_file_refs
from uwazi_admin_agent.domain.suggestion import SuggestionFilter
from uwazi_admin_agent.ports.backup_store_port import BackupStorePort
from uwazi_admin_agent.ports.cache_invalidation_port import CacheInvalidationPort
from uwazi_admin_agent.ports.drift_probe_port import DriftProbePort
from uwazi_admin_agent.ports.drift_store_port import DriftStorePort
from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort

# Probe page size: the fast check reads only the most recently edited entities.
DRIFT_PROBE_LIMIT: int = 10

# A resolver from the run's template NAME to its id (or None when the template
# is unknown/untemplated). Injected so the use case never reaches into the API
# adapter's ``.client`` attribute.
TemplateIdResolver = Callable[[str | None], str | None]


def unfiltered() -> SuggestionFilter:
    """The empty filter (every row of a run)."""
    return SuggestionFilter()


def languages_for_run(run_id: str, store: DriftStorePort) -> list[str]:
    """The DISTINCT languages among the run's rows (the languages the probe covers)."""
    languages = [lang for (_sid, lang) in store.entity_languages(run_id, unfiltered()) if lang]
    if not languages:
        return [DUMMY_LANGUAGE]
    distinct: list[str] = []
    for lang in languages:
        if lang not in distinct:
            distinct.append(lang)
    return distinct


def baseline_rows_from_store(store: DriftStorePort, run_id: str) -> dict[str, int | None]:
    """``sharedId -> max recorded editDate`` (None kept for never-seen entities)."""
    out: dict[str, int | None] = {}
    for row in store.drift_baseline(run_id):
        current = out.get(row.shared_id)
        if row.edit_date is not None and (current is None or row.edit_date > current):
            out[row.shared_id] = row.edit_date
        elif row.shared_id not in out:
            out[row.shared_id] = None
    return out


def baseline_max(store: DriftStorePort, run_id: str) -> int | None:
    """The baseline's max editDate overall (None when empty or nothing recorded)."""
    values = [row.edit_date for row in store.drift_baseline(run_id) if row.edit_date is not None]
    return max(values) if values else None


def dominant_template_name(store: DriftStorePort, run_id: str) -> str | None:
    """The most frequent template name among the run's rows (None when none)."""
    rows, _total = store.entity_rows(run_id, unfiltered())
    tally: dict[str, int] = {}
    for row in rows:
        for name in row.template_names:
            tally[name] = tally.get(name, 0) + 1
    if not tally:
        return None
    return max(tally.items(), key=lambda kv: kv[1])[0]


def file_drift_entries(
    store: DriftStorePort,
    entity_repository: EntityRepositoryPort | None,
    cache: CacheInvalidationPort | None,
    run_id: str,
) -> list[DriftEntry]:
    """The file-set drift of the sources a run's file operations snapshot.

    ``editDate`` cannot see a newly attached supporting file, so the review
    row's own ``merge_files`` snapshot is the baseline: every source named by a
    merge/move row is re-read and its live file identities are compared with
    what the table recorded.

    Reads are language-aware on purpose: each recorded file carries the entity
    row language it was read from, and only those rows are re-read (a source
    with no recorded files is read in the default language — what a
    language-less read recorded from). Comparing against EVERY run language
    would make a multi-language source look permanently stale whenever a row
    the move never reads holds files the table never planned to copy.

    Cost is one raw read per (source, recorded language), bounded by the run's
    file operations — NOT the template. The entity cache is invalidated first,
    because a cached raw predates a file-only change exactly like the editDate
    does. A source whose read fails is skipped (an unread source must never
    look like "all its files were deleted"). Called from worker threads only,
    so ``asyncio.run`` is safe.
    """
    rows = store.file_operation_rows(run_id)
    recorded = recorded_file_signatures(rows)
    if not recorded:
        return []
    if entity_repository is None:
        logger.warning("file drift: no entity repository — file snapshots not checked")
        return []
    languages_by_source = recorded_file_languages(rows)
    shared_ids = sorted(recorded)
    if cache is not None:
        cache.invalidate_entities(shared_ids)

    async def _read_live() -> tuple[dict[str, list[dict[str, Any]]], set[str]]:
        live: dict[str, list[dict[str, Any]]] = {}
        unreadable: set[str] = set()
        for shared_id in shared_ids:
            refs: list[dict[str, Any]] = []
            failed = False
            for language in sorted(languages_by_source.get(shared_id) or {DUMMY_LANGUAGE}):
                try:
                    raw = await entity_repository.get_raw_by_shared_id(shared_id, language)
                except Exception as exc:  # noqa: BLE001 — an unread source cannot prove drift
                    logger.warning("file drift: could not read {} ({}): {}", shared_id, language, exc)
                    failed = True
                    continue
                refs.extend(ref.model_dump() for ref in extract_file_refs(raw))
            if failed:
                unreadable.add(shared_id)
            else:
                live[shared_id] = refs
        return live, unreadable

    live, unreadable = asyncio.run(_read_live())
    compared = {source: signature for source, signature in recorded.items() if source not in unreadable}
    return [DriftEntry(shared_id=source, kind=DriftKind.FILES) for source in changed_file_entities(compared, live)]


def scan_drift_report(
    store: DriftStorePort,
    drift_probe: DriftProbePort,
    entity_repository: EntityRepositoryPort | None,
    cache: CacheInvalidationPort | None,
    backup_store: BackupStorePort,
    run_id: str,
    template_id: str,
    now: datetime,
) -> DriftReport:
    """Full paged diff against the baseline for one resolved template id."""
    manifest = backup_store.load_manifest(run_id)
    run_written = {e.shared_id for e in (*manifest.modified, *manifest.created, *manifest.deleted)}
    languages = languages_for_run(run_id, store)
    live: list[dict[str, Any]] = []
    scan_failed: list[str] = []
    for language in languages:
        try:
            scanned = drift_probe.scan_template(template_id, language)
        except Exception as exc:  # noqa: BLE001 — one language failing keeps the others
            logger.warning("table refresh drift scan failed {}: {}", language, exc)
            scan_failed.append(language)
            continue
        live.extend({"shared_id": shared_id, "edit_date": edit_date} for shared_id, edit_date in scanned.items())
    try:
        file_entries = file_drift_entries(store, entity_repository, cache, run_id)
    except Exception as exc:  # noqa: BLE001 — file drift is additive; the editDate diff still stands
        logger.warning("table refresh file drift failed: {}", exc)
        file_entries = []
    return build_report(
        live=live,
        baseline=baseline_rows_from_store(store, run_id),
        excluded_ids=run_written,
        languages_failed=scan_failed,
        checked_at=now,
        files=file_entries,
    )


def sync_drift_baseline(
    store: DriftStorePort,
    drift_probe: DriftProbePort | None,
    template_id_resolver: TemplateIdResolver,
    run_id: str,
) -> int:
    """Record ``(sharedId, language) -> editDate`` for the run's template snapshot.

    Paged ``/api/v2/search`` per covered language, editDate + sharedId only.
    Returns the number of baseline rows written. Called after generation and
    after each apply, so the baseline never flags the run's own writes.
    """
    if drift_probe is None:
        logger.warning("drift probe not wired — baseline not synced for {}", run_id)
        return 0
    template_name = dominant_template_name(store, run_id)
    template_id = template_id_resolver(template_name)
    now = datetime.now(timezone.utc)
    languages = languages_for_run(run_id, store)
    rows: list[DriftBaselineRow] = []
    try:
        if template_id:
            for language in languages:
                try:
                    scanned = drift_probe.scan_template(template_id, language)
                except Exception as exc:  # noqa: BLE001 — one language failing keeps the others
                    logger.warning("drift baseline scan failed {}: {}", language, exc)
                    continue
                for shared_id, edit_date in scanned.items():
                    rows.append(
                        DriftBaselineRow(
                            run_id=run_id,
                            shared_id=shared_id,
                            language=language,
                            edit_date=edit_date,
                            title=None,
                            captured_at=now,
                        )
                    )
            live_count = drift_probe.count_by_template(template_id)
        else:
            # No template in the rows: capture only the run's own (sharedId,
            # language) rows with no editDate — a probe can then never flag
            # them as drift (their baseline date is unknown, treated valid).
            for shared_id, lang in store.entity_languages(run_id, unfiltered()):
                rows.append(
                    DriftBaselineRow(
                        run_id=run_id,
                        shared_id=shared_id,
                        language=lang,
                        edit_date=None,
                        title=None,
                        captured_at=now,
                    )
                )
            live_count = None
    except Exception as exc:  # noqa: BLE001 — baseline failure leaves no drift verdict
        logger.warning("drift baseline sync failed for {}: {}", run_id, exc)
        return 0
    store.replace_drift_baseline(run_id, rows)
    # Reset knowledge of drift: the sync re-captured everything known.
    store.set_drift_state(
        run_id,
        status=DriftStatus.UNKNOWN,
        checked_at=now,
        checked_by="sync",
        live_count=live_count,
        detail="baseline synced",
    )
    return len(rows)


def check_drift(
    store: DriftStorePort,
    drift_probe: DriftProbePort | None,
    backup_store: BackupStorePort,
    entity_repository: EntityRepositoryPort | None,
    cache: CacheInvalidationPort | None,
    template_id_resolver: TemplateIdResolver,
    run_id: str,
    by: str = "auto",
) -> DriftStateRow:
    """Check whether Uwazi drifted since the baseline; persist + return the verdict.

    Cheap first (probe + count, one request per language + one count); only a
    flagged probe pays the full paged diff. Any failure flips the verdict to
    ERROR — the banner says so and the check can be retried; correctness never
    relies on a failed probe's silence.
    """
    now = datetime.now(timezone.utc)
    if drift_probe is None:
        state = DriftStateRow(
            run_id=run_id,
            status=DriftStatus.UNKNOWN,
            checked_at=now,
            checked_by=by,
            detail="drift probe not wired",
        )
        store.set_drift_state(
            run_id,
            status=state.status,
            checked_at=now,
            checked_by=by,
            detail=state.detail,
        )
        return state
    manifest = backup_store.load_manifest(run_id)
    run_written = {e.shared_id for e in (*manifest.modified, *manifest.created, *manifest.deleted)}

    baseline_max_edit_date = baseline_max(store, run_id)
    template_name = dominant_template_name(store, run_id)
    template_id = template_id_resolver(template_name)
    if template_id is None:
        state = DriftStateRow(
            run_id=run_id,
            status=DriftStatus.UNKNOWN,
            checked_at=now,
            checked_by=by,
            detail="no template recorded — drift cannot be probed",
        )
        store.set_drift_state(
            run_id,
            status=state.status,
            checked_at=now,
            checked_by=by,
            detail=state.detail,
        )
        return state
    languages = languages_for_run(run_id, store)
    try:
        probed: dict[str, int | None] = {}
        languages_failed: list[str] = []
        for language in languages:
            try:
                probed.update(drift_probe.probe_recent(template_id, language, DRIFT_PROBE_LIMIT))
            except Exception as exc:  # noqa: BLE001 — one language failing keeps the others
                logger.warning("drift probe failed {}: {}", language, exc)
                languages_failed.append(language)
        live_count: int | None = None
        count_failed = False
        try:
            live_count = drift_probe.count_by_template(template_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("drift count failed: {}", exc)
            count_failed = True

        baseline_map = baseline_rows_from_store(store, run_id)
        entries = build_probe_result(probed, baseline_map, run_written)
        baseline_count = store.drift_state(run_id)
        baseline_count_value = baseline_count.live_count if baseline_count is not None else None
        count_drift = live_count is not None and baseline_count_value is not None and live_count != baseline_count_value
        try:
            file_entries = file_drift_entries(store, entity_repository, cache, run_id)
        except Exception as exc:  # noqa: BLE001 — file drift is additive; editDate/count still decide
            logger.warning("file drift check failed: {}", exc)
            file_entries = []
        if entries or count_drift or file_entries:
            # Flagged: pay the full diff for the precise classification + counts.
            try:
                live: list[dict[str, Any]] = []
                scan_failed: list[str] = []
                for language in [lang for lang in languages if lang not in languages_failed]:
                    try:
                        scanned = drift_probe.scan_template(template_id, language)
                    except Exception:  # noqa: BLE001
                        scan_failed.append(language)
                        continue
                    live.extend({"shared_id": sid, "edit_date": dt} for sid, dt in scanned.items())
                report = build_report(
                    live=live,
                    baseline=baseline_map,
                    excluded_ids=run_written,
                    languages_failed=scan_failed,
                    checked_at=now,
                    files=file_entries,
                )
                detail = drift_detail(report)
                status = DriftStatus.DRIFTED
                live_count_stored = live_count if live_count is not None else baseline_count_value
                store.set_drift_state(
                    run_id,
                    status=status,
                    checked_at=now,
                    checked_by=by,
                    baseline_max_edit_date=baseline_max_edit_date,
                    live_count=live_count_stored,
                    detail=detail,
                )
                return store.drift_state(run_id) or DriftStateRow(run_id=run_id, status=status, checked_at=now)
            except Exception as exc:
                logger.warning("drift scan failed: {}", exc)
                store.set_drift_state(
                    run_id,
                    status=DriftStatus.DRIFTED,
                    checked_at=now,
                    checked_by=by,
                    baseline_max_edit_date=baseline_max_edit_date,
                    live_count=live_count if live_count is not None else baseline_count_value,
                    detail="drift detected (detail scan failed)",
                )
                return store.drift_state(run_id) or DriftStateRow(run_id=run_id, status=DriftStatus.DRIFTED, checked_at=now)

        if languages_failed or count_failed:
            status = DriftStatus.ERROR
            detail: str | None = f"probe failed ({', '.join([*languages_failed] or ['count'])}) — retry"
        else:
            status = DriftStatus.VALID
            detail = None
        store.set_drift_state(
            run_id,
            status=status,
            checked_at=now,
            checked_by=by,
            baseline_max_edit_date=baseline_max_edit_date,
            live_count=live_count if live_count is not None else baseline_count_value,
            detail=detail,
        )
        final = store.drift_state(run_id)
        return final or DriftStateRow(run_id=run_id, status=status, checked_at=now)
    except Exception as exc:  # noqa: BLE001 — a crash in the check is an ERROR verdict, not a silence
        logger.exception("drift check crashed")
        store.set_drift_state(
            run_id,
            status=DriftStatus.ERROR,
            checked_at=now,
            checked_by=by,
            detail=f"check crashed: {exc}",
        )
        final = store.drift_state(run_id)
        return final or DriftStateRow(run_id=run_id, status=DriftStatus.ERROR, checked_at=now)


def drift_detail(report: DriftReport) -> str:
    """The banner's one-line drift summary (pure)."""
    parts: list[str] = []
    if report.modified:
        parts.append(f"{len(report.modified)} {MODIFIED_LABEL.lower()}")
    if report.files:
        # The count is entities whose file set moved, not a file count: "1 file
        # changed" would be a lie when one source gained three attachments.
        count = len(report.files)
        parts.append(f"{count} {'entity' if count == 1 else 'entities'} with changed files in uwazi")
    if report.new:
        parts.append(f"{len(report.new)} {NEW_LABEL.lower()}")
    if report.missing:
        parts.append(f"{len(report.missing)} {MISSING_LABEL.lower()}")
    return ", ".join(parts)


def refresh_baseline_after_run(
    store: DriftStorePort,
    drift_probe: DriftProbePort | None,
    template_id_resolver: TemplateIdResolver,
    run_id: str,
) -> None:
    """After an apply: re-sync the baseline (best effort; never fail the apply)."""
    try:
        sync_drift_baseline(store, drift_probe, template_id_resolver, run_id)
    except Exception as exc:  # noqa: BLE001 — the apply succeeded; drift bookkeeping must not fail it
        logger.warning("post-run baseline sync failed for {}: {}", run_id, exc)
        store.drift_mark_stale(run_id)


__all__ = [
    "DRIFT_PROBE_LIMIT",
    "TemplateIdResolver",
    "baseline_max",
    "baseline_rows_from_store",
    "check_drift",
    "dominant_template_name",
    "drift_detail",
    "file_drift_entries",
    "languages_for_run",
    "refresh_baseline_after_run",
    "scan_drift_report",
    "sync_drift_baseline",
    "unfiltered",
]
