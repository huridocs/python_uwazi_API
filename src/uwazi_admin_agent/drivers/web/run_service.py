"""Async service layer for the admin agent web UI (Phase 5 driver).

Returns data (not prints) so NiceGUI's event loop can call mutating operations
in background tasks. Calls use cases directly — the step drivers
(``step_generate`` etc.) wrap each in ``asyncio.run()``, which is incompatible
with NiceGUI's already-running loop, so this module mirrors their wiring
without the ``asyncio.run`` wrapper.

No business logic: this is a driver that wires adapters to use cases, matching
the ``drivers/`` layer convention.
"""

import asyncio
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from loguru import logger
from pydantic import BaseModel, Field

from uwazi_admin_agent.adapters.audit_log_adapter import JsonlAuditLog
from uwazi_admin_agent.adapters.pydantic_ai_runtime import PydanticAiLlmRuntime
from uwazi_admin_agent.adapters.script_emitter import emit_generated_script
from uwazi_admin_agent.adapters.segmentation_repository_adapter import language_candidates
from uwazi_admin_agent.adapters.sqlite_suggestion_store import SqliteSuggestionStore
from uwazi_admin_agent.adapters.tasks_store import YamlTaskStore
from uwazi_admin_agent.configuration import (
    DUMMY_LANGUAGE,
    LLM_PRICE_INPUT_PER_MTOK,
    LLM_PRICE_OUTPUT_PER_MTOK,
    PROMPTS_PATH,
    RUNS_FILE,
    RUNS_PATH,
    SUGGESTIONS_DB_PATH,
    TASKS_PATH,
)
from uwazi_admin_agent.domain.audit_record import AuditOutcome, AuditRecord, AuditStep, make_audit_record
from uwazi_admin_agent.domain.deleted_file import DeletedFile
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
from uwazi_admin_agent.domain.file_drift import (
    changed_file_entities,
    recorded_file_languages,
    recorded_file_signatures,
)
from uwazi_admin_agent.domain.file_restore import extract_file_refs
from uwazi_admin_agent.domain.llm_budget import LlmBudget, LlmPrice
from uwazi_admin_agent.domain.manifest import AppliedWrite, MigrationManifest, RewiredRelationship, RunStatus
from uwazi_admin_agent.domain.prompt_chat import ChatMessage, ChatTurn, build_final_prompt_from_chat
from uwazi_admin_agent.domain.prompt_understanding import PromptUnderstanding
from uwazi_admin_agent.domain.revert_gate import RevertRefusedError
from uwazi_admin_agent.domain.revert_verification import format_verification_result
from uwazi_admin_agent.domain.review_refresh import (
    PreflightResult,
    ReconciliationSummary,
    evaluate_preflight,
)
from uwazi_admin_agent.domain.snapshot import EntityIdentity
from uwazi_admin_agent.domain.suggestion import (
    EntityReviewRow,
    OperationReviewRow,
    PredictionPass,
    Suggestion,
    SuggestionFilter,
)
from uwazi_admin_agent.domain.task import TaskView
from uwazi_admin_agent.drivers.runtime import build_audit_log, build_backup_store, build_file_cache, build_runtime
from uwazi_admin_agent.ports.audit_log_port import AuditLogPort
from uwazi_admin_agent.ports.backup_store_port import BackupStorePort
from uwazi_admin_agent.ports.suggestion_store_port import SuggestionStorePort
from uwazi_admin_agent.ports.task_store_port import TaskStorePort
from uwazi_admin_agent.use_cases.apply_suggestions_use_case import (
    ApplyRefusedError,
    ApplySuggestionsUseCase,
)
from uwazi_admin_agent.use_cases.clarify_prompt_use_case import ClarifyPromptUseCase
from uwazi_admin_agent.use_cases.generate_predictions_use_case import GeneratePredictionsUseCase, _fetch_raws
from uwazi_admin_agent.use_cases.generate_script_use_case import GenerateScriptUseCase
from uwazi_admin_agent.use_cases.improve_prompt_use_case import ImprovePromptUseCase
from uwazi_api.domain.segmentation import Segmentation


class GenerateError(Exception):
    """Generation failed; the manifest is persisted as GENERATION_FAILED."""


class RevertVerificationError(Exception):
    """Revert succeeded but post-revert verification found mismatches."""


@dataclass(frozen=True)
class RunSummary:
    """One row in the runs table — the manifest's headline fields."""

    run_id: str
    last_executed_at: datetime | None
    status: RunStatus
    created_at: datetime
    prompt: str
    modified: int
    deleted: int
    created: int
    rewired: int
    error: str | None


@dataclass(frozen=True)
class RunDetail:
    run_id: str
    status: RunStatus
    created_at: datetime
    prompt: str
    validated_prompt: str | None
    script: str
    modified: int
    deleted: int
    created: int
    rewired: int
    error: str | None


@dataclass(frozen=True)
class ExecutionEvent:
    """One row in a run's execution-history modal (derived from audit records)."""

    timestamp: datetime
    type: str  # "execute" | "revert"
    outcome: str  # "success" | "failure"
    detail: str | None


@dataclass(frozen=True)
class TableUpdateResult:
    """What one "Update table from Uwazi" pass changed."""

    run_id: str
    changed_in_uwazi: int | None
    reconciliation: ReconciliationSummary

    def message(self) -> str:
        if self.changed_in_uwazi is None:
            changed = "table refreshed (Uwazi change scan unavailable)"
        elif self.changed_in_uwazi:
            changed = f"{self.changed_in_uwazi} entity change(s) detected in Uwazi"
        else:
            changed = "no Uwazi changes detected"
        return f"Table updated: {changed}; {self.reconciliation.summary_text()}."


@dataclass(frozen=True)
class RunResults:
    """The touch-set a run produced: which entities/files it changed.

    Unlike :class:`RunDetail` (which reports counts for the table), this carries
    the full identity lists from the manifest so the UI can show exactly which
    entities were modified/created/deleted, which relationships were rewired,
    and which files were deleted. It also carries the script's ``result`` value
    (the answer for a query task, or the summary for a mutation task), the
    ``applied_writes`` (what each write did, with before-values — the diff
    source), and ``titles`` (shared_id → title, resolved without any HTTP call).
    """

    run_id: str
    status: RunStatus
    result: str | None
    modified: list[EntityIdentity]
    created: list[EntityIdentity]
    deleted: list[EntityIdentity]
    rewired: list[RewiredRelationship]
    deleted_files: list[DeletedFile]
    applied_writes: list[AppliedWrite]
    titles: dict[str, str]


def _touch_counts(manifest: MigrationManifest) -> tuple[int, int, int, int]:
    """(modified, deleted, created, rewired) counts shown in the table.

    A reverted run has no outstanding changes — everything was restored or
    deleted — so its counts read as 0 even though the manifest keeps the
    touch-set for post-revert verification and audit.
    """
    if manifest.status == RunStatus.REVERTED:
        return 0, 0, 0, 0
    return len(manifest.modified), len(manifest.deleted), len(manifest.created), len(manifest.rewired)


def list_runs() -> list[RunSummary]:
    """Return all persisted runs (skips folders whose manifest is unreadable)."""
    store = build_backup_store()
    summaries: list[RunSummary] = []
    for run_id in store.list_runs():
        try:
            manifest = store.load_manifest(run_id)
        except FileNotFoundError:
            continue
        modified, deleted, created, rewired = _touch_counts(manifest)
        summaries.append(
            RunSummary(
                run_id=manifest.run_id,
                status=manifest.status,
                created_at=manifest.created_at,
                last_executed_at=manifest.last_executed_at,
                prompt=manifest.prompt,
                modified=modified,
                deleted=deleted,
                created=created,
                rewired=rewired,
                error=manifest.error,
            )
        )
    return summaries


def get_run(run_id: str) -> RunDetail:
    """Return a run's manifest fields + the emitted script source."""
    store = build_backup_store()
    manifest = store.load_manifest(run_id)
    script_path = RUNS_PATH / run_id / "script.py"
    script = script_path.read_text(encoding="utf-8") if script_path.is_file() else manifest.script
    modified, deleted, created, rewired = _touch_counts(manifest)
    return RunDetail(
        run_id=manifest.run_id,
        status=manifest.status,
        created_at=manifest.created_at,
        prompt=manifest.prompt,
        validated_prompt=manifest.validated_prompt,
        script=script,
        modified=modified,
        deleted=deleted,
        created=created,
        rewired=rewired,
        error=manifest.error,
    )


def get_run_audit(run_id: str) -> list[AuditRecord]:
    """Return a run's full audit trail (empty when the run has no audit log)."""
    return build_audit_log().load(run_id)


def get_run_results(run_id: str) -> RunResults:
    """Return a run's touch-set (the entities/files it changed).

    Reads the manifest directly (not the count-reduced :func:`get_run`) so a
    reverted run still shows its full touch-set — the manifest keeps the lists
    for post-revert verification even though the table counts read as 0.

    Also resolves each touched entity's title with zero HTTP calls: recorded
    applied writes carry the title the script set, and anything still missing
    (a legacy run that recorded no writes, or an entity only touched by a
    rewire/delete) is read out of that entity's on-disk before-snapshot. A
    missing snapshot is skipped — the UI falls back to the shared id.
    """
    store = build_backup_store()
    manifest = store.load_manifest(run_id)
    return RunResults(
        run_id=manifest.run_id,
        status=manifest.status,
        result=manifest.result,
        modified=manifest.modified,
        created=manifest.created,
        deleted=manifest.deleted,
        rewired=manifest.rewired,
        deleted_files=manifest.deleted_files,
        applied_writes=manifest.applied_writes,
        titles=_resolve_titles(store, run_id, manifest),
    )


def _resolve_titles(store: BackupStorePort, run_id: str, manifest: MigrationManifest) -> dict[str, str]:
    """Build the shared_id → title map for a run, from run data only (no HTTP).

    Two sources, in order: the titles the applied writes recorded (what the
    script set, always current), then the raw ``title`` inside each touched
    entity's before-snapshot (covers legacy runs and entities this run only
    rewired or deleted). Entities created by this run have no snapshot — their
    title comes from the create's applied write.
    """
    titles: dict[str, str] = {}
    for write in manifest.applied_writes:
        if write.title:
            titles[write.shared_id] = write.title
    created_ids = {e.shared_id for e in manifest.created}
    touched = [e.shared_id for e in (*manifest.modified, *manifest.created, *manifest.deleted)]
    for shared_id in touched:
        if shared_id in titles or shared_id in created_ids:
            continue
        try:
            snapshot = store.load_snapshot(run_id, shared_id)
        except Exception:  # noqa: BLE001 — a missing/broken snapshot must not break the dialog
            continue
        title = (snapshot.raw or {}).get("title")
        if isinstance(title, str) and title:
            titles[shared_id] = title
    return titles


async def get_entity_segmentation(user: str, password: str, shared_id: str, language: str) -> Segmentation:
    """Fetch one entity's primary-document segmentation (the entity window's viewer).

    Resolves the entity's document for ``language`` on the controlled instance and
    returns its segmentation. The repository is the runtime's cache-wrapped
    :class:`SegmentationRepositoryPort`, so a repeat view is served from the
    persistent cache. Raises on any failure — the caller degrades to an error label.
    """
    runtime = build_runtime(user=user, password=password)
    return await runtime.deps.segmentation_repository.get_by_shared_id(shared_id, language)


@dataclass(frozen=True)
class EntityDocument:
    """An entity's PRIMARY document and the language it was found in.

    ``language`` is the language whose entity row actually carries the file,
    which is not necessarily the one that was asked for: an entity usually
    holds its document in the language it was uploaded in.
    """

    filename: str
    language: str


def document_language_candidates(requested: str | None, configured: list[str]) -> list[str]:
    """Order languages to try for an entity's document (pure).

    The requested language first, then the instance's configured languages —
    the same fallback the segmentation resolver uses, over the same pure
    helper, so a document and its segmentation are always resolved in the same
    language. With no request, the instance's languages are the whole order.
    """
    return language_candidates(requested or DUMMY_LANGUAGE, configured)


async def _instance_languages(runtime: Any) -> list[str]:
    """The instance's configured language keys (``[]`` when settings are unreadable)."""
    settings = getattr(runtime, "settings_repository", None)
    if settings is None:
        return []
    try:
        languages = await asyncio.to_thread(settings.get_languages)
    except Exception as exc:  # noqa: BLE001 — the fallback order is best-effort
        logger.info("could not read the instance's configured languages: {}", exc)
        return []
    return [str(language.key) for language in languages if getattr(language, "key", None)]


async def resolve_entity_document(
    user: str, password: str, shared_id: str, language: str | None = None
) -> EntityDocument | None:
    """The entity's primary document (and the language it lives in), or None.

    Resolution order: the requested language, then every language the instance
    is configured for. A row's document is frequently uploaded in a language
    other than the one the row is written in (a translated text is updated in
    ``es`` while the PDF was uploaded in ``en``), so a single-language lookup
    finds nothing and the reviewer sees a blank panel. None means the entity
    has no uploaded primary document in ANY configured language — the caller
    then falls back to the document text instead of showing an empty viewer.
    """
    runtime = build_runtime(user=user, password=password)
    candidates = document_language_candidates(language, await _instance_languages(runtime))
    for candidate in candidates:
        try:
            raw = await runtime.entity_repository.get_raw_by_shared_id(shared_id, candidate)
        except Exception as exc:  # noqa: BLE001 — no locale row for this language
            logger.info("no entity raw for sharedId={} language={}: {}", shared_id, candidate, exc)
            continue
        documents = [ref for ref in extract_file_refs(raw) if ref.kind == "document"]
        if documents:
            return EntityDocument(documents[0].filename, candidate)
    return None


async def fetch_entity_document_bytes(user: str, password: str, shared_id: str, language: str | None = None) -> bytes | None:
    """Fetch an entity's PRIMARY document bytes for the review window's viewer.

    Resolves the file the same way :func:`resolve_entity_document` does (so the
    viewer's bytes and the window's viewer-or-text decision can never disagree)
    and returns them through the cached file repository — re-opening the same
    entity is free. ``None`` when the entity has no uploaded primary document.
    """
    runtime = build_runtime(user=user, password=password)
    document = await resolve_entity_document(user, password, shared_id, language)
    if document is None:
        return None
    return await runtime.file_repository.get_file_bytes(document.filename)


def _run_level_events(records: list[AuditRecord]) -> list[AuditRecord]:
    """Keep only run-level lifecycle audit records (execute / revert).

    Per-entity writes (update, delete, restore_*, …) carry ``shared_ids`` and are
    excluded; the run-level records emitted by the execute/revert use cases use
    ``op_kind`` ``"execute"`` / ``"revert"`` with empty ``shared_ids``.
    """
    return [r for r in records if r.op_kind in ("execute", "revert")]


def get_execution_history(run_id: str, audit_log: AuditLogPort | None = None) -> list[ExecutionEvent]:
    """Return a run's execute/revert events, newest first, for the history modal.

    Reads the run's on-disk audit log (``<RUNS_PATH>/<run_id>/audit.jsonl``) unless
    an ``audit_log`` is injected (used by tests); a run with no audit log (never
    executed, or pre-Phase 6) yields an empty list.
    """
    log = audit_log if audit_log is not None else JsonlAuditLog(RUNS_PATH)
    audit = log.load(run_id)
    events = _run_level_events(audit)
    events.sort(key=lambda r: r.timestamp, reverse=True)
    return [
        ExecutionEvent(
            timestamp=r.timestamp,
            type=r.op_kind,
            outcome=r.outcome.value,
            detail=r.detail,
        )
        for r in events
    ]


async def clarify_prompt(prompt: str, user: str, password: str) -> PromptUnderstanding:
    """Run the prompt-validation step: restate the prompt + surface ambiguities.

    A lightweight, tool-free LLM call that runs BEFORE generation so the operator
    can confirm/rephrase. Raises on LLM failure; the caller shows a skip affordance.
    """
    runtime = build_runtime(user=user, password=password)
    use_case = ClarifyPromptUseCase(llm=runtime.llm, deps=runtime.deps)
    return await use_case.execute(prompt)


async def create_and_generate(
    name: str,
    prompt: str,
    user: str,
    password: str,
    validated_prompt: str | None = None,
    budget: LlmBudget | None = None,
) -> None:
    """Write the prompt + active-run pointer, then generate the script + the table.

    ``prompt`` is the operator's original request; ``validated_prompt`` (when
    given) is the final, validated prompt actually sent to the generation LLM.
    ``budget`` is the operator-approved cap on the generated script's own
    llm_complete/llm_map calls (the wizard's caps step); it governs the
    automatic planning pass that fills the review table. On generation failure
    the manifest is persisted with ``status=GENERATION_FAILED`` (the prompt
    yaml stays on disk for retry) and :class:`GenerateError` is raised so the
    caller shows a short toast; the full detail lives on the manifest.
    """
    PROMPTS_PATH.mkdir(parents=True, exist_ok=True)
    (PROMPTS_PATH / f"{name}.yaml").write_text(yaml.dump({"prompt": prompt}), encoding="utf-8")
    RUNS_FILE.parent.mkdir(parents=True, exist_ok=True)
    RUNS_FILE.write_text(yaml.dump({"active_run": name}), encoding="utf-8")

    # Resolve the run folder directly from the name: resolving through the
    # RUNS_FILE pointer (load_active_path) is a TOCTOU race — two concurrent
    # generations would both read whatever pointer was written last and emit
    # into the WRONG run folder. RUNS_FILE stays written for CLI compatibility.
    run_path = RUNS_PATH / name
    run_path.mkdir(parents=True, exist_ok=True)
    prompt_snapshot = PROMPTS_PATH / f"{name}.yaml"
    if prompt_snapshot.is_file():
        shutil.copy2(prompt_snapshot, run_path / prompt_snapshot.name)

    # Building the runtime LOGS IN: it logs into the controlled Uwazi instance, so
    # an unreachable instance, a bad UWAZI_URL or bad credentials fail here —
    # before the try below exists and before any manifest is written. Without the
    # log the operator saw only the toast and the terminal stayed silent.
    try:
        runtime = build_runtime(user=user, password=password)
    except Exception as exc:
        logger.exception("run {}: could not build the Uwazi runtime", name)
        raise GenerateError(
            f"could not connect to the Uwazi instance ({exc}). Check UWAZI_URL and the Uwazi credentials."
        ) from exc
    use_case = GenerateScriptUseCase(
        llm=runtime.llm,
        deps=runtime.deps,
    )

    final_prompt = validated_prompt or prompt
    try:
        script = await use_case.execute(final_prompt)
        run_path.mkdir(parents=True, exist_ok=True)
        emit_generated_script(script, run_path)
        _persist_generation_history(run_path, use_case)
    except Exception as exc:
        manifest = MigrationManifest(
            run_id=name,
            created_at=datetime.now(timezone.utc),
            prompt=prompt,
            validated_prompt=validated_prompt,
            script="",
            status=RunStatus.GENERATION_FAILED,
            snapshot_dir=str(run_path),
            error=str(exc),
            error_step="generate",
        )
        runtime.backup_store.save_manifest(name, manifest)
        raise GenerateError(str(exc)) from exc

    manifest = MigrationManifest(
        run_id=name,
        created_at=datetime.now(timezone.utc),
        prompt=prompt,
        validated_prompt=validated_prompt,
        script=script.python_code,
        status=RunStatus.PLANNED,
        snapshot_dir=str(run_path),
    )
    runtime.backup_store.save_manifest(name, manifest)

    # The planning scan runs AUTOMATICALLY, unasked: the script cannot call the
    # LLM in this pass (it is bound no-LLM dry-run namespace), so nothing here
    # costs money — the review table's rows are simply built the moment the
    # script exists.
    await run_auto_scan(name, user, password, budget)


async def run_auto_scan(run_id: str, user: str, password: str, budget: LlmBudget | None = None) -> ReconciliationSummary:
    """Run the PLANNING pass over ``run_id``'s script and fill the review table.

    The ONE generated script runs in the recorded-write namespace with the LLM
    helpers bound under ``budget`` — the caps the operator approved at task
    creation (the wizard's caps step); ``None`` falls back to the configured
    script-caps defaults. A breach aborts the pass cleanly, keeping earlier
    rows. There is ONE live table per run: the pass MERGES its rows into it
    (rows the script still produces keep their identity and status, changed
    rows go back to pending, rows it no longer produces become obsolete).
    Errors propagate: the same path the wizard shows a toast for. Returns the
    reconciliation summary of the merge into the live table.
    """
    summary = await generate_predictions(run_id, user, password, budget or _script_caps_budget(), None)
    # The rows just recorded are Uwazi's CURRENT state: capture their editDates
    # as the drift baseline now, so a later manual edit is detectable. Failure
    # is non-fatal (the table is still built).
    refresh_baseline_after_run(run_id, user, password)
    return summary


def _script_caps_budget() -> LlmBudget:
    """The configured default caps for the script's llm tool calls.

    Used when the wizard's budget wasn't passed (older callers/CLI): the same
    defaults the wizard's caps step prefills. The pass's ledger enforces them
    ONLY against the script's llm_complete/llm_map usage.
    """
    from uwazi_admin_agent.configuration import SCRIPT_DEFAULT_MAX_COST, SCRIPT_DEFAULT_MAX_REQUESTS

    return LlmBudget(
        max_cost=SCRIPT_DEFAULT_MAX_COST,
        max_requests=SCRIPT_DEFAULT_MAX_REQUESTS,
        price=LlmPrice(
            model="script-caps",
            input_per_mtok=LLM_PRICE_INPUT_PER_MTOK,
            output_per_mtok=LLM_PRICE_OUTPUT_PER_MTOK,
        ),
    )


GENERATION_HISTORY_FILENAME: str = "generation_messages.json"


def _persist_generation_history(run_path: Path, use_case: GenerateScriptUseCase) -> None:
    """Write the generation conversation next to the run's script.

    The review UI's "Improve prompt" loop needs the ORIGINAL discovery context to
    repair the script without re-exploring; ``all_messages_json`` is that context
    in a round-trippable form. Best-effort: a serialization failure only loses
    the cheap-repair path (the loop falls back to a fresh generation).
    """
    payload = getattr(use_case, "_last_messages_json", None)
    if not payload:
        return
    try:
        (run_path / GENERATION_HISTORY_FILENAME).write_bytes(payload)
    except OSError as exc:  # noqa: BLE001 — never fail a generation over this
        logger.warning("could not persist generation history for {}: {}", run_path.name, exc)


def has_generation_history(run_id: str) -> bool:
    """True when a run has a persisted generation conversation (repair is cheap)."""
    return (RUNS_PATH / run_id / GENERATION_HISTORY_FILENAME).is_file()


def build_suggestion_store() -> SuggestionStorePort:
    """Return the shared SQLite suggestion store (schema ensured on build)."""
    return SqliteSuggestionStore(SUGGESTIONS_DB_PATH)


def read_run_script(run_id: str) -> str:
    """Read a run's script source (the ONE stable path the emitter writes).

    There is one script per run: ``script.py`` is overwritten when a
    regeneration produces a new one, so the live table always reflects the
    script that produced it.
    """
    return (RUNS_PATH / run_id / "script.py").read_text(encoding="utf-8")


async def generate_predictions(
    run_id: str,
    user: str,
    password: str,
    budget: LlmBudget,
    shared_ids: list[str] | None = None,
) -> ReconciliationSummary:
    """Run the planning pass over a run's script and persist its rows.

    A subset pass passes ``shared_ids``; None means the script discovers its own
    scope. The pass cannot mutate Uwazi (every write is a recorded no-op). The
    script's OWN llm_complete/llm_map calls ride the LLM runtime under the
    ledger built from ``budget`` — the caps the operator approved at task
    creation; a breach aborts the pass keeping earlier rows.

    Returns the reconciliation summary of the merge into the ONE live table.
    """
    runtime = build_runtime(user=user, password=password)
    script = read_run_script(run_id)
    use_case = GeneratePredictionsUseCase(
        entity_api=runtime.entity_api,
        entity_repository=runtime.entity_repository,
        file_repository=runtime.file_repository,
        segmentation_repository=runtime.segmentation_repository,
        url_fetcher=runtime.url_fetcher,
        suggestion_store=build_suggestion_store(),
        llm_runtime=PydanticAiLlmRuntime(runtime.llm),
        default_language=DUMMY_LANGUAGE,
        template_labels=runtime.template_labels,
        translation_port=runtime.translation_port,
        # The template property TYPES: the recording path needs them to tell a
        # per-language change from a shared one (a multiselect change is ONE
        # row, a translated paragraph is one row per language).
        property_types=runtime.property_types,
        llm_cache=runtime.file_cache,
    )
    await use_case.run(script, run_id=run_id, budget=budget, entity_scope=shared_ids)
    return use_case.last_reconciliation or ReconciliationSummary()


async def update_table_from_uwazi(run_id: str, user: str, password: str) -> TableUpdateResult:
    """Refresh the review table in place against current Uwazi state.

    Full script pass with the persistent caches: LLM/translation answers for
    unchanged inputs are served from disk, while the entities Uwazi reports as
    changed, new, or deleted have their cached raws invalidated first so the
    pass sees live data. The same pass row and the same suggestion rows are
    kept; :meth:`GeneratePredictionsUseCase.refresh` merges by identity.
    """
    store = build_suggestion_store()
    assert isinstance(store, SqliteSuggestionStore)
    pass_row = current_pass(run_id)
    if pass_row is None:
        raise GenerateError(f"run {run_id!r} has no prediction pass to refresh")

    runtime = await asyncio.to_thread(build_runtime, user, password)
    script = read_run_script(run_id)
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
        changed_ids = set(store.shared_ids(run_id, SuggestionFilter(include_obsolete=True)))
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
    _final, summary = await use_case.refresh(
        script,
        run_id=run_id,
        budget=pass_row.budget or _script_caps_budget(),
    )

    baseline_rows = await asyncio.to_thread(
        sync_drift_baseline,
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
            baseline_max_edit_date=_baseline_max(store, run_id),
            # Keep the count sync_drift_baseline just captured: clearing it
            # would make the next probe blind to count-only drift.
            live_count=synced_state.live_count if synced_state is not None else None,
            detail=None,
        )
    try:
        build_audit_log().append(
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
    )


def get_table_update_time(run_id: str) -> datetime | None:
    """When the table was last refreshed from Uwazi (None = never)."""
    records = build_audit_log().load(run_id)
    timestamps = [record.timestamp for record in records if record.step is AuditStep.TABLE_SYNC]
    return max(timestamps) if timestamps else None


def preflight_selection(
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
    store = build_suggestion_store()
    pass_row = current_pass(run_id)
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
    runtime = build_runtime(user=user, password=password)
    pairs = {
        (row.shared_id, row.language or DUMMY_LANGUAGE)
        for row in rows
        if row.shared_id and not row.shared_id.startswith("dry-created-")
    }
    raws = _fetch_raws(runtime.entity_repository, pairs) if pairs else {}
    return evaluate_preflight(rows, raws, DUMMY_LANGUAGE)


def _file_drift_entries(
    runtime: Any,
    store: SqliteSuggestionStore,
    run_id: str,
) -> list[DriftEntry]:
    """The file-set drift of the sources a run's file operations snapshot (I/O).

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
    look like "all its files were deleted"). Called from worker threads only
    (``check_drift`` / ``_scan_drift_report``), so ``asyncio.run`` is safe.
    """
    rows = store.file_operation_rows(run_id)
    recorded = recorded_file_signatures(rows)
    if not recorded:
        return []
    repository = getattr(runtime, "entity_repository", None)
    if repository is None:
        logger.warning("file drift: no entity repository — file snapshots not checked")
        return []
    languages_by_source = recorded_file_languages(rows)
    shared_ids = sorted(recorded)
    cache = getattr(runtime, "file_cache", None)
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
                    raw = await repository.get_raw_by_shared_id(shared_id, language)
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


def _scan_drift_report(
    runtime: Any,
    store: SqliteSuggestionStore,
    run_id: str,
    now: datetime,
) -> DriftReport | None:
    """Full paged diff against the baseline (None = no template to scan)."""
    if runtime.drift_probe is None:
        return None
    template_name = _dominant_template_name(store, run_id)
    template_id = _check_template_id(runtime, template_name)
    if template_id is None:
        return None
    manifest = runtime.backup_store.load_manifest(run_id)
    run_written = {e.shared_id for e in (*manifest.modified, *manifest.created, *manifest.deleted)}
    languages = _languages_for_run(run_id, store)
    live: list[dict[str, Any]] = []
    scan_failed: list[str] = []
    for language in languages:
        try:
            scanned = runtime.drift_probe.scan_template(template_id, language)
        except Exception as exc:  # noqa: BLE001 — one language failing keeps the others
            logger.warning("table refresh drift scan failed {}: {}", language, exc)
            scan_failed.append(language)
            continue
        live.extend({"shared_id": shared_id, "edit_date": edit_date} for shared_id, edit_date in scanned.items())
    try:
        file_entries = _file_drift_entries(runtime, store, run_id)
    except Exception as exc:  # noqa: BLE001 — file drift is additive; the editDate diff still stands
        logger.warning("table refresh file drift failed: {}", exc)
        file_entries = []
    return build_report(
        live=live,
        baseline=_baseline_rows_from_store(store, run_id),
        excluded_ids=run_written,
        languages_failed=scan_failed,
        checked_at=now,
        files=file_entries,
    )


def get_suggestions(run_id: str, f: SuggestionFilter) -> tuple[list[Suggestion], int]:
    """Return a page of suggestions + the filter's total (the review grid's read)."""
    return build_suggestion_store().query(run_id, f)


def current_pass(run_id: str) -> PredictionPass | None:
    """The run's single prediction pass row, or None when none is recorded.

    The review page never carries a pass number — the table is ONE live table
    per run — so the review reads below resolve the current pass HERE. A later
    pass overwrites this row, and that is the one the review surface follows.
    """
    return build_suggestion_store().get_pass(run_id)


def get_suggestion_entity_languages(run_id: str, f: SuggestionFilter) -> list[tuple[str, str | None]]:
    """Return every (sharedId, language) row matching a filter (the window's prev/next)."""
    return build_suggestion_store().entity_languages(run_id, f)


def get_suggestion_counts(run_id: str) -> dict[str, int]:
    """Return the review header's aggregate counters (SQL-side group-bys).

    The status keys count ENTITY-LANGUAGE GROUPS — the rows the review table shows
    — so the cards and the table agree; ``match_*`` stay row-level.
    """
    return build_suggestion_store().counts(run_id)


def get_suggestion_properties(run_id: str) -> list[str]:
    """Return the distinct property names in a set (the property dropdown)."""
    return build_suggestion_store().property_names(run_id)


def get_entity_suggestions(
    run_id: str,
    shared_id: str,
    language: str | None = None,
    f: SuggestionFilter | None = None,
) -> list[Suggestion]:
    """Return the suggestions of one entity's language group (the entity window's read).

    ``f`` is the review table's current filter, so the detail window shows the
    same properties the table does; ``language=None`` reads the
    language-independent group, exactly as the table's own rows do.
    """
    return build_suggestion_store().get_entity(run_id, shared_id, language, f)


# Property labels belong to the TEMPLATE, not to a run, so they are resolved once
# per (account, template) and reused: opening a second entity of the same template
# — or coming back to the same one — costs no request and no login. Keyed by
# account so two operators never read a map built with another one's credentials.
_PROPERTY_LABELS: dict[tuple[str, str], dict[str, str]] = {}


def property_labels_for(template_name: str | None, user: str, password: str) -> dict[str, str]:
    """``{property_name: label}`` for one template, cached; ``{}`` when unresolvable.

    A suggestion row names its property by the template's property ``name``
    (``document_ref``), which reads as an identifier on screen; Uwazi shows the
    property's ``label`` ("Document Ref"), and so does the entity window. The
    lookup goes through the template-label port (one cached ``/api/templates``
    read behind it) and degrades to ``{}`` — the caller keeps showing the raw
    names — whenever the instance cannot be reached or the template is unknown, so
    a display path never depends on it.
    """
    if not template_name:
        return {}
    key = (user, template_name)
    cached = _PROPERTY_LABELS.get(key)
    if cached is not None:
        return cached
    labels: dict[str, str] = {}
    try:
        runtime = build_runtime(user=user, password=password)
        labels = dict(runtime.template_labels.property_labels(template_name)) if runtime.template_labels else {}
    except Exception as exc:  # noqa: BLE001 — a display lookup never blocks the window
        logger.warning("could not resolve property labels for template {}: {}", template_name, exc)
    _PROPERTY_LABELS[key] = labels
    return labels


def get_entity_rows(run_id: str, f: SuggestionFilter) -> tuple[list[EntityReviewRow], int]:
    """Return a page of per-entity aggregate rows + the distinct-entity total.

    The entity table's read: one row per affected entity (checkbox column feeds
    the selection-driven actions), grouped in SQL like every other review read.
    """
    return build_suggestion_store().entity_rows(run_id, f)


def get_operation_rows(run_id: str, f: SuggestionFilter) -> tuple[list[OperationReviewRow], int]:
    """Return a page of OPERATIONS (one row per entity) with their suboperations.

    The review table's read: the filter is a SUBOPERATION filter, so an operation
    whose every part the filter removed never reaches the page. ``subs`` carries
    the page's matching suggestion rows.
    """
    return build_suggestion_store().operations(run_id, f)


def get_property_scores(run_id: str) -> dict[str, tuple[int, int]]:
    """``property -> (exact, mismatch)`` over the whole run (the Property filter)."""
    return build_suggestion_store().property_scores(run_id)


def get_templates(run_id: str) -> list[str]:
    """The distinct template names of the run — the property-label lookup."""
    return build_suggestion_store().templates(run_id)


def get_suggestion_languages(run_id: str) -> list[str]:
    """The distinct languages the run writes — the Language filter's options."""
    return build_suggestion_store().languages(run_id)


def get_selection_rows(run_id: str, f: SuggestionFilter) -> list[tuple[int, str, str | None, str]]:
    """Every matching row's ``(id, sharedId, language, op)`` — the whole-result selection."""
    return build_suggestion_store().selection_rows(run_id, f)


def discard_rows(run_id: str, pairs: list[tuple[str, str | None]], reason: str | None = None) -> int:
    """Mark the given entity-language groups' rows DISCARDED; return how many moved.

    The review table's Discard action, per row and over the checked selection:
    "the end user does not want this executed". The rows are marked, not
    deleted — a Run skips a discarded row (it accepts only PENDING/REVERTED
    ones), and the operator can bring the operation back with the Undo button.

    The groups are addressed as ``(sharedId, language)`` pairs, the table's own
    row identity, so discarding a translation's ``es`` row leaves its ``en``
    sibling alone. An empty list discards nothing.
    """
    store = build_suggestion_store()
    return store.discard_by_filter(
        run_id,
        SuggestionFilter(entity_languages=pairs),
        reason,
    )


def restore_rows(run_id: str, pairs: list[tuple[str, str | None]]) -> int:
    """Undo a discard: the groups' DISCARDED rows go back to PENDING; return the count.

    The counterpart of :func:`discard_rows` — an operation the operator marked
    as discarded becomes runnable again.
    """
    store = build_suggestion_store()
    return store.restore_by_filter(
        run_id,
        SuggestionFilter(entity_languages=pairs),
    )


def discard_ids(run_id: str, ids: list[int], reason: str | None = None) -> int:
    """Mark exactly these SUBOPERATIONS discarded; return how many moved.

    The table's Discard acts on checked suboperation rows, so the selection is a
    suggestion-id list — narrowing a discard to one property of one
    entity-language group, which the pair-based :func:`discard_rows` cannot
    express. APPLIED rows are left alone (as in every discard path: un-writing
    is the revert's job). An empty list discards nothing.
    """
    if not ids:
        return 0
    store = build_suggestion_store()
    return store.discard_by_filter(
        run_id,
        SuggestionFilter(suggestion_ids=ids),
        reason,
    )


def restore_ids(run_id: str, ids: list[int]) -> int:
    """Undo the discard of exactly these SUBOPERATIONS; return the count."""
    if not ids:
        return 0
    store = build_suggestion_store()
    return store.restore_by_filter(
        run_id,
        SuggestionFilter(suggestion_ids=ids),
    )


def _apply_use_case(runtime: Any) -> ApplySuggestionsUseCase:
    """Wire the apply use case over a built runtime (the Run heuristic).

    No LLM wiring: the heuristic executes precomputed rows only — no script
    runs, no model calls, no budget to approve at write time.
    """
    return ApplySuggestionsUseCase(
        entity_api=runtime.entity_api,
        relationship_api=runtime.relationship_api,
        entity_repository=runtime.entity_repository,
        backup_store=runtime.backup_store,
        audit_log=runtime.audit_log,
        suggestion_store=build_suggestion_store(),
        file_repository=runtime.file_repository,
        segmentation_repository=runtime.segmentation_repository,
        url_fetcher=runtime.url_fetcher,
        cache_control=runtime.file_cache,
        default_language=DUMMY_LANGUAGE,
        publish_state=getattr(runtime, "publish_state", None),
    )


async def apply_outcomes(
    run_id: str,
    user: str,
    password: str,
    selection: list[tuple[str, str | None]],
    row_ids: list[int] | None = None,
    progress: Any | None = None,
) -> list[str]:
    """Execute the selected entities' rows into Uwazi (revertable).

    The Run action: the deterministic heuristic over the precomputed rows —
    property rows into per-entity update payloads, creates into create
    payloads, delete/publish/unpublish/set_publish_status/relationship rows
    through their dedicated intercepted helpers. The writes land in the run's
    OWN accumulating manifest with pre-change snapshots: a Run adds no row to
    the tasks table, it only changes the suggestion statuses and the run's
    status, which is what lets one whole-task revert undo it.
    Returns the run ids touched (the run itself).

    The Run stands in for the row-level accept: the use case accepts the
    selected rows' PENDING state as it pushes them, so the rows the review
    table shows (recorded by the planning pass) are what gets written.
    ``selection`` is the table's checked (sharedId, language) rows, so a
    translation task writes only the languages the operator checked.
    ``row_ids`` narrows the Run to the checked SUBOPERATIONS (the table's
    per-property rows): running one property of one entity-language group
    writes exactly that property and leaves its siblings pending.
    ``progress`` is the optional ``(done, total)`` callback the gate's toast
    polls — the SUBOPERATION fraction the operator watches while it runs.

    Raises :class:`ApplyRefusedError` when there is nothing to run.
    """
    if not selection:
        raise ApplyRefusedError("nothing selected to run")

    def _work() -> list[str]:
        runtime = build_runtime(user=user, password=password)
        use_case = _apply_use_case(runtime)
        use_case.apply(run_id, selection, row_ids, progress=progress)
        return [run_id]

    return await asyncio.to_thread(_work)


async def improve_prompt(
    run_id: str,
    history: list[ChatMessage],
    request: str,
    user: str,
    password: str,
    budget: LlmBudget | None = None,
) -> ChatTurn:
    """Run one prompt-improvement chat turn over ``history`` + ``request``.

    The chat's engine: a tool-free LLM call that sees the whole conversation
    and answers with what it understood, the clarifying questions it still has,
    and the COMPLETE revised prompt it proposes. Raises on LLM failure; the
    caller shows the error in the chat.

    ``budget`` carries the operator's approved caps from the cost dialog: its
    request backstop is the ENFORCED limit for the turn.
    """
    runtime = build_runtime(user=user, password=password)
    use_case = ImprovePromptUseCase(llm=runtime.llm, deps=runtime.deps)
    max_requests = budget.max_requests if budget is not None else None
    return await use_case.execute(history, request, max_requests=max_requests)


async def regenerate_from_chat(
    run_id: str,
    history: list[ChatMessage],
    user: str,
    password: str,
    budget: LlmBudget | None = None,
) -> TableUpdateResult:
    """Regenerate the run's script from the whole chat history and reconcile the table.

    The chat's Run button. The steps, in order:

    1. fold the WHOLE chat into ONE prompt (the original + the resolved
       clarifications + every turn) — that is what the generation LLM receives;
    2. regenerate the script, reusing the run's persisted generation history so
       the ORIGINAL discovery context stays in view (a run generated before the
       history existed falls back to a fresh generation);
    3. overwrite the run's ONE script file (``script.py``);
    4. persist the new generation conversation (so the NEXT turn repairs against
       this turn's context);
    5. update the manifest — including ``validated_prompt``, which is what the
       prompt modal shows as the run's current prompt;
    6. re-scan: the planning pass runs the NEW script and MERGES its rows into
       the ONE live table (rows the script still produces keep their identity
       and status, changed rows go back to pending, rows it no longer produces
       become obsolete).

    Returns the reconciliation summary. Errors propagate: the caller shows them.
    """
    runtime = build_runtime(user=user, password=password)
    manifest = runtime.backup_store.load_manifest(run_id)
    run_path = RUNS_PATH / run_id
    final_prompt = build_final_prompt_from_chat(history)
    use_case = GenerateScriptUseCase(
        llm=runtime.llm,
        deps=runtime.deps,
    )
    history_path = run_path / GENERATION_HISTORY_FILENAME
    reused_history = False
    if history_path.is_file():
        try:
            use_case.load_history_json(history_path.read_bytes())
            reused_history = True
        except Exception as exc:  # noqa: BLE001 — fall back to a fresh generation
            logger.warning("could not restore generation history for {}: {}", run_id, exc)
    try:
        if reused_history:
            script = await use_case.repair(final_prompt)
        else:
            script = await use_case.execute(final_prompt)
    except Exception as exc:  # noqa: BLE001 — surface as a generation error
        raise GenerateError(str(exc)) from exc

    emit_generated_script(script, run_path)
    _persist_generation_history(run_path, use_case)
    manifest.script = script.python_code
    manifest.status = RunStatus.PLANNED
    manifest.error = None
    manifest.error_step = None
    manifest.validated_prompt = final_prompt
    runtime.backup_store.save_manifest(run_id, manifest)
    logger.info(
        "regenerated from chat run={} reused_history={}",
        run_id,
        reused_history,
    )
    # The regenerated script needs a planning pass: it merges its rows into the
    # ONE live table.
    summary = await run_auto_scan(run_id, user, password, budget)
    return TableUpdateResult(
        run_id=run_id,
        changed_in_uwazi=None,
        reconciliation=summary,
    )


# --- multistep tasks ---------------------------------------------------------


def build_task_store() -> TaskStorePort:
    """Return the YAML task store under ``TASKS_PATH``."""
    return YamlTaskStore(TASKS_PATH)


def _task_use_case() -> Any:
    """Wire the task use case over the existing single-run functions.

    The task layer is pure orchestration, so it is handed the very same
    revert/create functions the single-run flow uses — one code
    path for the actual work, no second implementation to keep in sync.
    """
    from uwazi_admin_agent.use_cases.task_use_case import TaskUseCase

    return TaskUseCase(
        task_store=build_task_store(),
        backup_store=build_backup_store(),
        revert_run=revert_run,
        create_run=create_and_generate,
    )


def task_view(task_id: str) -> TaskView:
    """A task's live view: steps, statuses, next executable step, revert plan."""
    return _task_use_case().get_task(task_id)


class EntityBackupInfo(BaseModel):
    """A run's backup record for one entity (the revert button's tooltip source)."""

    model_config = {"frozen": True}

    shared_id: str = Field(description="The entity the backup covers.")
    backup_created_at: datetime | None = Field(
        default=None,
        description="When the entity's single first-touch backup was captured (None = not backed up).",
    )
    languages: list[str] = Field(
        default_factory=list,
        description="The language rows the backup captured (the revert restores each of them).",
    )
    affected_languages: list[str] = Field(
        default_factory=list,
        description="The languages the run was writing when the backup was taken.",
    )


def entity_backup_info(run_id: str, shared_id: str) -> EntityBackupInfo:
    """One entity's backup metadata for a run (the per-row revert tooltip).

    Reads the snapshot file directly (no network). A legacy/missing snapshot
    returns ``backup_created_at=None``, and the UI shows the revert button
    without a tooltip date only when a backup actually exists.
    """
    store = build_backup_store()
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


async def revert_entity_rows(run_id: str, shared_id: str, user: str, password: str) -> EntityBackupInfo:
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
    runtime = build_runtime(user=user, password=password)
    snapshot = await runtime.revert_use_case.revert_entity(run_id, shared_id)
    if snapshot is None:
        raise RevertRefusedError("this row has no backup — the run has not written to this entity")
    return entity_backup_info(run_id, shared_id)


async def revert_suboperation(
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
    runtime = build_runtime(user=user, password=password)
    return await runtime.revert_use_case.revert_property(run_id, shared_id, language, property_name, suggestion_id)


class BulkRevertOutcome(BaseModel):
    """Result of reverting the checked rows' entities (the toast's summary)."""

    model_config = {"frozen": True}

    run_id: str = Field(description="The run the selected rows belong to.")
    reverted: list[str] = Field(
        default_factory=list,
        description="The sharedIds restored to their backup, in the order they were processed.",
    )
    skipped_created: list[str] = Field(
        default_factory=list,
        description="Selected entities the run itself created — their undo is a delete (run-level Revert), refused here.",
    )
    skipped_deleted: list[str] = Field(
        default_factory=list,
        description=(
            "Selected entities the run DELETED — their undo is a re-create, which only the ordered "
            "run-level revert can do (fresh sharedId + files + connection rows), refused here."
        ),
    )
    skipped_no_backup: list[str] = Field(
        default_factory=list,
        description="Selected entities with no backup — the run never wrote to them.",
    )
    failures: list[str] = Field(
        default_factory=list,
        description="Entities whose restore raised (restores that DID land stay reverted).",
    )

    def summary(self) -> str:
        """The operator-facing one-liner (pure)."""
        parts = [f"{len(self.reverted)} reverted"]
        if self.skipped_created:
            parts.append(f"{len(self.skipped_created)} created-by-run (revert at run level)")
        if self.skipped_deleted:
            parts.append(f"{len(self.skipped_deleted)} deleted-by-run (revert at run level)")
        if self.skipped_no_backup:
            parts.append(f"{len(self.skipped_no_backup)} without backup")
        if self.failures:
            parts.append(f"{len(self.failures)} failed")
        return ", ".join(parts)


async def revert_entities_selection(
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
    runtime = build_runtime(user=user, password=password)
    store = build_suggestion_store()
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


async def revert_run(run_id: str, user: str, password: str) -> None:
    """Revert a run and verify the restore.

    Raises :class:`RevertRefusedError` on refusal and
    :class:`RevertVerificationError` when the revert succeeded but the
    post-revert verification found mismatches (persisted on the manifest).
    """
    runtime = build_runtime(user=user, password=password)
    await runtime.revert_use_case.revert(run_id)
    result = await runtime.verify_use_case.verify(run_id)
    if not result.ok:
        detail = format_verification_result(result)
        manifest = runtime.backup_store.load_manifest(run_id)
        manifest.error = detail
        manifest.error_step = "verify"
        runtime.backup_store.save_manifest(run_id, manifest)
        raise RevertVerificationError(detail)


def uwazi_base_url() -> str | None:
    """The controlled Uwazi instance's base URL, or None when unconfigured.

    The review table links an entity to its public page on that instance, which
    needs the same ``UWAZI_URL`` the login screen caches. It lives here rather
    than in ``app`` because ``app`` already imports the review page, so reading
    the env var here avoids an import cycle.
    """
    return os.environ.get("UWAZI_URL") or None


def delete_run(run_id: str) -> None:
    """Remove a run's entire folder from the backup store + its suggestion rows."""
    build_backup_store().delete_run(run_id)
    # The suggestion DB is one shared file, so a run's rows do not disappear
    # with its folder: they are deleted explicitly here.
    build_suggestion_store().delete_run(run_id)


def clear_cache() -> int:
    """Clear the persistent file/entity cache for the configured instance.

    Returns the number of entries removed (0 when the cache is disabled or
    empty). Forces the next read to re-fetch server truth after direct human
    edits in Uwazi, which bypass write-path invalidation and are otherwise
    bounded only by the raw TTL.
    """
    url = os.environ.get("UWAZI_URL")
    if not url:
        return 0
    cache = build_file_cache(url)
    if cache is None:
        return 0
    return cache.clear()


def rename_run(old_id: str, new_id: str) -> None:
    """Rename a run end-to-end.

    Moves the run folder + manifest (via the backup store), relocates its prompt
    YAML under ``PROMPTS_PATH``, and repoints ``active_run.yaml`` if it targets
    the old id. ``new_id`` must be a non-empty, filesystem-safe name (no path
    separators or ``..``); the store raises if the target already exists.
    """
    new_id = (new_id or "").strip()
    if not new_id:
        raise ValueError("New run name is required")
    if "/" in new_id or "\\" in new_id or new_id in (".", ".."):
        raise ValueError(f"Invalid run name: {new_id!r}")
    # Relocate the prompt YAML if one exists for the old id.
    old_prompt = PROMPTS_PATH / f"{old_id}.yaml"
    new_prompt = PROMPTS_PATH / f"{new_id}.yaml"
    if old_prompt.is_file():
        old_prompt.rename(new_prompt)
    # Repoint the active-run pointer if it targets the old id.
    if RUNS_FILE.is_file():
        data = yaml.safe_load(RUNS_FILE.read_text(encoding="utf-8")) or {}
        if isinstance(data, dict) and data.get("active_run") == old_id:
            data["active_run"] = new_id
            RUNS_FILE.write_text(yaml.dump(data), encoding="utf-8")
    build_backup_store().rename_run(old_id, new_id)
    # The review table, the prediction pass and drift rows live in the shared
    # SQLite database keyed by run_id — renaming only the folder left the
    # renamed run's review page empty and the old id's rows orphaned.
    build_suggestion_store().rename_run(old_id, new_id)


# --- drift -------------------------------------------------------------------

# Probe page size: the fast check reads only the most recently edited entities.
DRIFT_PROBE_LIMIT: int = 10


def _check_template_id(runtime: Any, template_name: str | None) -> str | None:
    """Resolve the run's template name to its id (None = unresolvable/untemplated)."""
    if not template_name:
        return None
    try:
        template = runtime.entity_api.client.templates.get_by_name(template_name)
    except Exception as exc:  # noqa: BLE001 — an unresolvable template disables the probe
        logger.warning("could not resolve template {}: {}", template_name, exc)
        return None
    return template.id if template is not None else None


def _languages_for_run(run_id: str, store: SuggestionStorePort) -> list[str]:
    """The DISTINCT languages among the run's rows (the languages the probe covers)."""
    languages = [
        lang
        for (_sid, lang) in store.entity_languages(
            run_id,
            _unfiltered(),
        )
        if lang
    ]
    if not languages:
        return [DUMMY_LANGUAGE]
    distinct: list[str] = []
    for lang in languages:
        if lang not in distinct:
            distinct.append(lang)
    return distinct


def _unfiltered() -> SuggestionFilter:
    return SuggestionFilter()


def _baseline_rows_from_store(store: SqliteSuggestionStore, run_id: str) -> dict[str, int | None]:
    """``sharedId -> max recorded editDate`` (None kept for never-seen entities)."""
    out: dict[str, int | None] = {}
    for row in store.drift_baseline(run_id):
        current = out.get(row.shared_id)
        if row.edit_date is not None and (current is None or row.edit_date > current):
            out[row.shared_id] = row.edit_date
        elif row.shared_id not in out:
            out[row.shared_id] = None
    return out


def _baseline_max(store: SqliteSuggestionStore, run_id: str) -> int | None:
    """The baseline's max editDate overall (None when empty or nothing recorded)."""
    values = [row.edit_date for row in store.drift_baseline(run_id) if row.edit_date is not None]
    return max(values) if values else None


def sync_drift_baseline(
    run_id: str,
    user: str,
    password: str,
) -> int:
    """Record (sharedId, language) → editDate for the run's template snapshot.

    Paged ``/api/v2/search`` per covered language, editDate + sharedId only.
    Returns the number of baseline rows written. Called after generation and
    after each apply, so the baseline never flags the run's own writes.
    """
    store = build_suggestion_store()
    assert isinstance(store, SqliteSuggestionStore)
    runtime = build_runtime(user=user, password=password)
    if runtime.drift_probe is None:
        logger.warning("drift probe not wired — baseline not synced for {}", run_id)
        return 0
    template_name = _dominant_template_name(store, run_id)
    template_id = _check_template_id(runtime, template_name)
    now = datetime.now(timezone.utc)
    languages = _languages_for_run(run_id, store)
    rows: list[DriftBaselineRow] = []
    try:
        if template_id:
            for language in languages:
                try:
                    scanned = runtime.drift_probe.scan_template(template_id, language)
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
            live_count = runtime.drift_probe.count_by_template(template_id)
        else:
            # No template in the rows: capture only the run's own (sharedId,
            # language) rows with no editDate — a probe can then never flag
            # them as drift (their baseline date is unknown, treated valid).
            for shared_id, lang in store.entity_languages(run_id, _unfiltered()):
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


def _dominant_template_name(store: SuggestionStorePort, run_id: str) -> str | None:
    """The most frequent template name among the run's rows (None when none)."""
    rows, _total = store.entity_rows(run_id, _unfiltered())
    tally: dict[str, int] = {}
    for row in rows:
        for name in row.template_names:
            tally[name] = tally.get(name, 0) + 1
    if not tally:
        return None
    return max(tally.items(), key=lambda kv: kv[1])[0]


def get_drift_state(run_id: str) -> DriftStateRow | None:
    """The run's last-check outcome (the banner reads this)."""
    return build_suggestion_store().drift_state(run_id)


def check_drift(
    run_id: str,
    user: str,
    password: str,
    by: str = "auto",
) -> DriftStateRow:
    """Check whether Uwazi drifted since the baseline; persist + return the verdict.

    Cheap first (probe + count, one request per language + one count); only a
    flagged probe pays the full paged diff. Any failure flips the verdict to
    ERROR — the banner says so and the check can be retried; correctness never
    relies on a failed probe's silence.
    """
    store = build_suggestion_store()
    assert isinstance(store, SqliteSuggestionStore)
    runtime = build_runtime(user=user, password=password)
    now = datetime.now(timezone.utc)
    if runtime.drift_probe is None:
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
    manifest = runtime.backup_store.load_manifest(run_id)
    run_written = {e.shared_id for e in (*manifest.modified, *manifest.created, *manifest.deleted)}

    baseline_max = _baseline_max(store, run_id)
    template_name = _dominant_template_name(store, run_id)
    template_id = _check_template_id(runtime, template_name)
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
    languages = _languages_for_run(run_id, store)
    try:
        probed: dict[str, int | None] = {}
        languages_failed: list[str] = []
        for language in languages:
            try:
                probed.update(runtime.drift_probe.probe_recent(template_id, language, DRIFT_PROBE_LIMIT))
            except Exception as exc:  # noqa: BLE001 — one language failing keeps the others
                logger.warning("drift probe failed {}: {}", language, exc)
                languages_failed.append(language)
        live_count: int | None = None
        count_failed = False
        try:
            live_count = runtime.drift_probe.count_by_template(template_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("drift count failed: {}", exc)
            count_failed = True

        baseline_map = _baseline_rows_from_store(store, run_id)
        entries = build_probe_result(probed, baseline_map, run_written)
        baseline_count = store.drift_state(run_id)
        baseline_count_value = baseline_count.live_count if baseline_count is not None else None
        count_drift = live_count is not None and baseline_count_value is not None and live_count != baseline_count_value
        try:
            file_entries = _file_drift_entries(runtime, store, run_id)
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
                        scanned = runtime.drift_probe.scan_template(template_id, language)
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
                detail = _drift_detail(report)
                status = DriftStatus.DRIFTED
                live_count_stored = live_count if live_count is not None else baseline_count_value
                store.set_drift_state(
                    run_id,
                    status=status,
                    checked_at=now,
                    checked_by=by,
                    baseline_max_edit_date=baseline_max,
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
                    baseline_max_edit_date=baseline_max,
                    live_count=live_count if live_count is not None else baseline_count_value,
                    detail="drift detected (detail scan failed)",
                )
                return store.drift_state(run_id) or DriftStateRow(run_id=run_id, status=DriftStatus.DRIFTED, checked_at=now)

        if languages_failed or count_failed:
            status = DriftStatus.ERROR
            detail = f"probe failed ({', '.join([*languages_failed] or ['count'])}) — retry"
        else:
            status = DriftStatus.VALID
            detail = None
        store.set_drift_state(
            run_id,
            status=status,
            checked_at=now,
            checked_by=by,
            baseline_max_edit_date=baseline_max,
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


def _drift_detail(report: DriftReport) -> str:
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


def refresh_baseline_after_run(run_id: str, user: str, password: str) -> None:
    """After an apply: re-sync the baseline for the run's template (best effort)."""
    try:
        sync_drift_baseline(run_id, user, password)
    except Exception as exc:  # noqa: BLE001 — the apply succeeded; drift bookkeeping must not fail it
        logger.warning("post-run baseline sync failed for {}: {}", run_id, exc)
        build_suggestion_store().drift_mark_stale(run_id)


__all__ = [
    "EntityDocument",
    "GenerateError",
    "RevertVerificationError",
    "RunDetail",
    "RunResults",
    "RunSummary",
    "apply_outcomes",
    "build_suggestion_store",
    "check_drift",
    "clarify_prompt",
    "clear_cache",
    "create_and_generate",
    "current_pass",
    "delete_run",
    "document_language_candidates",
    "generate_predictions",
    "get_drift_state",
    "get_entity_rows",
    "get_entity_segmentation",
    "get_entity_suggestions",
    "get_run",
    "get_run_audit",
    "get_run_results",
    "get_suggestion_counts",
    "get_suggestion_entity_languages",
    "get_suggestion_properties",
    "get_suggestions",
    "get_templates",
    "list_runs",
    "property_labels_for",
    "read_run_script",
    "refresh_baseline_after_run",
    "rename_run",
    "revert_entities_selection",
    "revert_run",
    "run_auto_scan",
    "sync_drift_baseline",
    "uwazi_base_url",
]
