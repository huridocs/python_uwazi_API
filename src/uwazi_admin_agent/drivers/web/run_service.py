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
from uwazi_admin_agent.domain.audit_record import AuditRecord
from uwazi_admin_agent.domain.deleted_file import DeletedFile
from uwazi_admin_agent.domain.file_restore import extract_file_refs
from uwazi_admin_agent.domain.llm_budget import LlmBudget, LlmPrice
from uwazi_admin_agent.domain.manifest import AppliedWrite, MigrationManifest, RewiredRelationship, RunStatus
from uwazi_admin_agent.domain.prompt_understanding import PromptUnderstanding
from uwazi_admin_agent.domain.revert_verification import format_verification_result
from uwazi_admin_agent.domain.snapshot import EntityIdentity
from uwazi_admin_agent.domain.suggestion import (
    EntityReviewRow,
    PredictionAttempt,
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
from uwazi_admin_agent.use_cases.generate_predictions_use_case import GeneratePredictionsUseCase
from uwazi_admin_agent.use_cases.generate_script_use_case import GenerateScriptUseCase
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
    """Fetch one entity's primary-document segmentation (the results window's viewer).

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


async def run_auto_scan(run_id: str, user: str, password: str, budget: LlmBudget | None = None) -> int:
    """Run the PLANNING pass over ``run_id``'s script and fill the review table.

    The ONE generated script runs in the recorded-write namespace with the LLM
    helpers bound under ``budget`` — the caps the operator approved at task
    creation (the wizard's caps step); ``None`` falls back to the configured
    script-caps defaults. A breach aborts the pass cleanly, keeping earlier
    rows. It lands in the NEXT free attempt slot (a repair that regenerates
    the script gets a fresh attempt; earlier attempts' rows and review
    statuses are untouched). Returns the attempt number the pass used. Errors
    propagate: the same path the wizard shows a toast for.
    """
    attempt = _next_suggestion_attempt(run_id)
    await generate_predictions(run_id, user, password, attempt, budget or _script_caps_budget(), None)
    return attempt


def _next_suggestion_attempt(run_id: str) -> int:
    """One past the highest recorded prediction attempt (1 when none exist)."""
    return max((a.attempt for a in build_suggestion_store().list_attempts(run_id)), default=0) + 1


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


def read_run_script(run_id: str, attempt: int | None = None) -> str:
    """Read a run's script source (the attempt file when ``attempt`` is given).

    Attempts are immutable history (``script_attempt_<N>.py``); ``script.py``
    always holds the ORIGINAL generation, so an untouched run has no attempt
    files at all and every attempt reads ``script.py``.
    """
    run_path = RUNS_PATH / run_id
    if attempt is not None:
        candidate = run_path / f"script_attempt_{attempt}.py"
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    return (run_path / "script.py").read_text(encoding="utf-8")


async def generate_predictions(
    run_id: str,
    user: str,
    password: str,
    attempt: int,
    budget: LlmBudget,
    shared_ids: list[str] | None = None,
) -> None:
    """Run the planning pass over a run's script and persist its rows.

    A subset pass passes ``shared_ids``; None means the script discovers its own
    scope. The pass cannot mutate Uwazi (every write is a recorded no-op). The
    script's OWN llm_complete/llm_map calls ride the LLM runtime under the
    ledger built from ``budget`` — the caps the operator approved at task
    creation; a breach aborts the pass keeping earlier rows.
    """
    runtime = build_runtime(user=user, password=password)
    script = read_run_script(run_id, attempt)
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
    )
    await use_case.run(script, run_id=run_id, attempt=attempt, budget=budget, entity_scope=shared_ids)


def get_suggestions(run_id: str, attempt: int, f: SuggestionFilter) -> tuple[list[Suggestion], int]:
    """Return a page of suggestions + the filter's total (the review grid's read)."""
    return build_suggestion_store().query(run_id, attempt, f)


def get_attempt(run_id: str, attempt: int) -> PredictionAttempt | None:
    """Return one prediction attempt row (the progress/abort badge source)."""
    return build_suggestion_store().get_attempt(run_id, attempt)


def list_attempts(run_id: str) -> list[PredictionAttempt]:
    """Return every prediction attempt for a run, newest first."""
    return build_suggestion_store().list_attempts(run_id)


def current_attempt(run_id: str) -> PredictionAttempt | None:
    """The run's NEWEST prediction attempt row, or None when none is recorded.

    The review page no longer offers an attempt selector, so the review reads
    below resolve the current attempt HERE — the page never carries an attempt
    number. A later pass (a repair turn) becomes the current attempt by itself,
    and that is the one the review surface follows.
    """
    return next(iter(build_suggestion_store().list_attempts(run_id)), None)


def _current_attempt_number(run_id: str) -> int:
    """The attempt number the review reads use (1 before anything is recorded)."""
    attempt = current_attempt(run_id)
    return attempt.attempt if attempt is not None else 1


def get_suggestion_entity_languages(run_id: str, f: SuggestionFilter) -> list[tuple[str, str | None]]:
    """Return every (sharedId, language) row matching a filter (the window's prev/next)."""
    return build_suggestion_store().entity_languages(run_id, _current_attempt_number(run_id), f)


def get_suggestion_counts(run_id: str) -> dict[str, int]:
    """Return the review header's aggregate counters (SQL-side group-bys).

    The status keys count ENTITY-LANGUAGE GROUPS — the rows the review table shows
    — so the cards and the table agree; ``match_*`` stay row-level.
    """
    return build_suggestion_store().counts(run_id, _current_attempt_number(run_id))


def get_suggestion_properties(run_id: str) -> list[str]:
    """Return the distinct property names in a set (the property dropdown)."""
    return build_suggestion_store().property_names(run_id, _current_attempt_number(run_id))


def get_entity_suggestions(run_id: str, shared_id: str, language: str | None = None) -> list[Suggestion]:
    """Return the suggestions of one entity's language group (the entity window's read)."""
    return build_suggestion_store().get_entity(run_id, _current_attempt_number(run_id), shared_id, language)


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
    return build_suggestion_store().entity_rows(run_id, _current_attempt_number(run_id), f)


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
    )


async def apply_outcomes(
    run_id: str,
    user: str,
    password: str,
    selection: list[tuple[str, str | None]],
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

    Raises :class:`ApplyRefusedError` when there is nothing to run.
    """
    if not selection:
        raise ApplyRefusedError("nothing selected to run")

    def _work() -> list[str]:
        runtime = build_runtime(user=user, password=password)
        use_case = _apply_use_case(runtime)
        use_case.apply(run_id, _current_attempt_number(run_id), selection)
        return [run_id]

    return await asyncio.to_thread(_work)


async def repair_predictions(
    run_id: str,
    feedback: str,
    user: str,
    password: str,
    budget: LlmBudget,
) -> str:
    """Run a repair turn on the run's script and save it as a new attempt.

    The review UI's prompt-revisit loop: the discard reasons and mismatch
    samples become ``feedback``, and the script is regenerated with the ORIGINAL
    generation context still in view (``repair`` reuses the persisted message
    history, so discovery is not repeated). The new script is written as
    ``script_attempt_<N>.py`` — the original ``script.py`` is never overwritten,
    so attempts stay immutable history. Returns the new script path as a string.

    When no history was persisted (a run generated before this feature), the
    turn falls back to a FRESH generation over the original prompt, which the UI
    reports so the operator knows re-discovery will be paid for.
    """
    runtime = build_runtime(user=user, password=password)
    manifest = runtime.backup_store.load_manifest(run_id)
    run_path = RUNS_PATH / run_id
    use_case = GenerateScriptUseCase(
        llm=runtime.llm,
        deps=runtime.deps,
        entity_repository=runtime.entity_repository,
    )
    history_path = run_path / GENERATION_HISTORY_FILENAME
    repaired_online = False
    if history_path.is_file():
        try:
            use_case.load_history_json(history_path.read_bytes())
            repaired_online = True
        except Exception as exc:  # noqa: BLE001 — fall back to a fresh generation
            logger.warning("could not restore generation history for {}: {}", run_id, exc)
    try:
        if repaired_online:
            script = await use_case.repair(feedback)
        else:
            script = await use_case.execute(f"{manifest.validated_prompt or manifest.prompt}\n\n{feedback}")
    except Exception as exc:  # noqa: BLE001 — surface as a generation error
        raise GenerateError(str(exc)) from exc

    attempt = _next_script_attempt(run_path)
    (run_path / f"script_attempt_{attempt}.py").write_text(script.python_code, encoding="utf-8")
    _persist_generation_history(run_path, use_case)
    manifest.script = script.python_code
    manifest.status = RunStatus.PLANNED
    manifest.error = None
    manifest.error_step = None
    runtime.backup_store.save_manifest(run_id, manifest)
    logger.info(
        "repaired predictions run={} attempt={} reused_history={}",
        run_id,
        attempt,
        repaired_online,
    )
    # The regenerated script needs a fresh planning scan to build its rows:
    # it runs unasked into a new attempt slot.
    await run_auto_scan(run_id, user, password)
    return str(run_path / f"script_attempt_{attempt}.py")


def _next_script_attempt(run_path: Path) -> int:
    """The next attempt number for a run's script history (1 when none exist)."""
    existing = [p for p in run_path.glob("script_attempt_*.py")]
    numbers = []
    for path in existing:
        try:
            numbers.append(int(path.stem.removeprefix("script_attempt_")))
        except ValueError:
            continue
    return max(numbers, default=0) + 1


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


__all__ = [
    "EntityDocument",
    "GenerateError",
    "RevertVerificationError",
    "RunDetail",
    "RunResults",
    "RunSummary",
    "apply_outcomes",
    "build_suggestion_store",
    "clarify_prompt",
    "clear_cache",
    "create_and_generate",
    "current_attempt",
    "delete_run",
    "document_language_candidates",
    "generate_predictions",
    "get_attempt",
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
    "list_attempts",
    "list_runs",
    "property_labels_for",
    "read_run_script",
    "revert_run",
    "rename_run",
    "run_auto_scan",
    "uwazi_base_url",
]
