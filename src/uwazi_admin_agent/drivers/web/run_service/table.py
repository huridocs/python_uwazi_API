"""Web driver module split out of the original ``run_service`` (behavior preserved)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from uwazi_admin_agent.adapters.pydantic_ai_runtime import PydanticAiLlmRuntime
from uwazi_admin_agent.adapters.sqlite_suggestion_store import SqliteSuggestionStore
from uwazi_admin_agent.configuration import (
    DUMMY_LANGUAGE,
)
from uwazi_admin_agent.domain.audit_record import AuditOutcome, AuditStep, make_audit_record
from uwazi_admin_agent.domain.drift import DriftReport, DriftStatus
from uwazi_admin_agent.domain.file_drift import recorded_file_signatures
from uwazi_admin_agent.domain.review_refresh import (
    PreflightResult,
    evaluate_preflight,
)
from uwazi_admin_agent.domain.suggestion import (
    Suggestion,
    SuggestionFilter,
)
from uwazi_admin_agent.drivers.runtime import build_audit_log, build_runtime
from uwazi_admin_agent.use_cases import drift_service
from uwazi_admin_agent.use_cases.generate_predictions_use_case import (
    GeneratePredictionsUseCase,
    _fetch_raws,
)

from .drift import _template_id, sync_drift_baseline
from .models import GenerateError, TableUpdateResult
from .review import current_pass
from .wiring import _script_caps_budget, build_suggestion_store, read_run_script


async def update_table_from_uwazi(url: str, run_id: str, user: str, password: str) -> TableUpdateResult:
    """Refresh the review table in place against current Uwazi state.

    Full script pass with the persistent caches: LLM/translation answers for
    unchanged inputs are served from disk, while the entities Uwazi reports as
    changed, new, or deleted have their cached raws invalidated first so the
    pass sees live data. The same pass row and the same suggestion rows are
    kept; :meth:`GeneratePredictionsUseCase.refresh` merges by identity.
    """
    store = build_suggestion_store(url)
    assert isinstance(store, SqliteSuggestionStore)
    pass_row = current_pass(url, run_id)
    if pass_row is None:
        raise GenerateError(f"run {run_id!r} has no prediction pass to refresh")

    runtime = await asyncio.to_thread(build_runtime, user, password, url)
    script = read_run_script(url, run_id)
    now = datetime.now(timezone.utc)
    report = await asyncio.to_thread(
        _scan_drift_report,
        runtime,
        store,
        run_id,
        now,
    )
    if report is not None:
        changed_ids = {entry.shared_id for entry in report.entries()}
        changed_in_uwazi: int | None = report.total()
    else:
        # Without a scan we cannot name what changed. Invalidate everything the
        # run already knows about (rows + baseline) so no stale raw can be
        # mistaken for live state; new entities would not be cached anyway.
        changed_ids = set(store.shared_ids(run_id, SuggestionFilter()))
        changed_ids.update(row.shared_id for row in store.drift_baseline(run_id))
        changed_in_uwazi = None
    # The file operations' sources are ALWAYS re-read: attaching a supporting
    # file does not bump editDate, so the drift scan can miss it, yet the
    # script's get_entity_files would keep serving the cached raw without the
    # attachment. Invalidate them explicitly so the refresh sees the new file.
    file_sources = set(recorded_file_signatures(store.file_operation_rows(run_id)))
    if runtime.file_cache is not None and (changed_ids or file_sources):
        # Missing entities too: a cached raw would otherwise make the script
        # propose writes for an entity that no longer exists.
        runtime.file_cache.invalidate_entities(sorted(changed_ids | file_sources))

    use_case = GeneratePredictionsUseCase(
        entity_api=runtime.entity_api,
        entity_repository=runtime.entity_repository,
        file_repository=runtime.file_repository,
        segmentation_repository=runtime.segmentation_repository,
        url_fetcher=runtime.url_fetcher,
        suggestion_store=store,
        llm_runtime=PydanticAiLlmRuntime(runtime.llm),
        default_language=DUMMY_LANGUAGE,
        template_labels=runtime.template_labels,
        translation_port=runtime.translation_port,
        property_types=runtime.property_types,
        llm_cache=runtime.file_cache,
    )
    from .validation import holdout_ids

    _final, summary = await use_case.run(
        script,
        run_id=run_id,
        budget=pass_row.budget or _script_caps_budget(),
        plan_kind=runtime.backup_store.load_manifest(run_id).plan_kind,
        exclude_scope=holdout_ids(url, run_id) or None,
    )

    baseline_rows = await asyncio.to_thread(
        sync_drift_baseline,
        url,
        run_id,
        user,
        password,
    )
    if baseline_rows:
        synced_state = store.drift_state(run_id)
        store.set_drift_state(
            run_id,
            status=DriftStatus.VALID,
            checked_at=datetime.now(timezone.utc),
            checked_by=user,
            baseline_max_edit_date=drift_service.baseline_max(store, run_id),
            # Keep the count sync_drift_baseline just captured: clearing it
            # would make the next probe blind to count-only drift.
            live_count=synced_state.live_count if synced_state is not None else None,
            detail=None,
        )
    try:
        build_audit_log(url).append(
            run_id,
            make_audit_record(
                run_id,
                AuditStep.TABLE_SYNC,
                "table_sync",
                [],
                AuditOutcome.SUCCESS,
                detail=summary.summary_text(),
            ),
        )
    except Exception as exc:  # noqa: BLE001 — the refresh succeeded; logging is best-effort
        logger.warning("could not record the table-update timestamp for {}: {}", run_id, exc)
    return TableUpdateResult(
        run_id=run_id,
        changed_in_uwazi=changed_in_uwazi,
        reconciliation=summary,
        outcome=use_case.last_plan_outcome,
    )


def preflight_selection(
    url: str,
    run_id: str,
    suggestion_ids: list[int],
    user: str,
    password: str,
) -> PreflightResult:
    """Check selected rows against live Uwazi before a Run touches anything.

    Fetches each selected entity-language raw once, then compares the stored
    ``current_value`` with the live value (or checks existence for whole-entity
    ops). Creates and non-runnable rows are ignored by the pure evaluator.
    """
    if not suggestion_ids:
        return PreflightResult()
    store = build_suggestion_store(url)
    pass_row = current_pass(url, run_id)
    if pass_row is None:
        return PreflightResult()
    rows: list[Suggestion] = []
    chunk_size = 500
    for start in range(0, len(suggestion_ids), chunk_size):
        chunk = suggestion_ids[start : start + chunk_size]
        page, _total = store.query(
            run_id,
            SuggestionFilter(suggestion_ids=chunk, limit=len(chunk)),
        )
        rows.extend(page)
    runtime = build_runtime(user=user, password=password, url=url)
    pairs = {
        (row.shared_id, row.language or DUMMY_LANGUAGE)
        for row in rows
        if row.shared_id and not row.shared_id.startswith("dry-created-")
    }
    raws = _fetch_raws(runtime.entity_repository, pairs) if pairs else {}
    return evaluate_preflight(rows, raws, DUMMY_LANGUAGE)


def _scan_drift_report(
    runtime: Any,
    store: SqliteSuggestionStore,
    run_id: str,
    now: datetime,
) -> DriftReport | None:
    """Full paged diff against the baseline (None = no template to scan).

    Thin wrapper around :func:`use_cases.drift_service.scan_drift_report`; the
    driver owns only the runtime wiring.
    """
    if runtime.drift_probe is None:
        return None
    template_id = _template_id(runtime, drift_service.dominant_template_name(store, run_id))
    if template_id is None:
        return None
    return drift_service.scan_drift_report(
        store,
        runtime.drift_probe,
        runtime.entity_repository,
        runtime.file_cache,
        runtime.backup_store,
        run_id,
        template_id,
        now,
    )
