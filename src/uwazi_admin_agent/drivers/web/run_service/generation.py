"""Web driver module split out of the original ``run_service`` (behavior preserved)."""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Callable
from datetime import datetime, timezone

import yaml
from loguru import logger

from uwazi_admin_agent.adapters.pydantic_ai_runtime import PydanticAiLlmRuntime
from uwazi_admin_agent.adapters.script_emitter import emit_generated_script
from uwazi_admin_agent.configuration import (
    DUMMY_LANGUAGE,
)
from uwazi_admin_agent.domain.llm_budget import LlmBudget
from uwazi_admin_agent.domain.manifest import MigrationManifest, RunStatus
from uwazi_admin_agent.domain.plan_intent import PlanOutcome
from uwazi_admin_agent.domain.prompt_chat import ChatMessage, build_final_prompt_from_chat
from uwazi_admin_agent.domain.review_refresh import (
    ReconciliationSummary,
)
from uwazi_admin_agent.domain.run_name import validate_run_name
from uwazi_admin_agent.domain.suggestion import (
    DISPLAY_ONLY_OPS,
    INFO_OP,
    MATCHED_OP,
    SKIPPED_OP,
    SuggestionFilter,
)
from uwazi_admin_agent.drivers.runtime import build_runtime
from uwazi_admin_agent.use_cases.generate_predictions_use_case import (
    GeneratePredictionsUseCase,
    PredictionPassError,
)
from uwazi_admin_agent.use_cases.generate_script_use_case import GenerateScriptUseCase

from .drift import refresh_baseline_after_run
from .models import GenerateError, TableUpdateResult
from .wiring import _paths, _script_caps_budget, build_suggestion_store, read_run_script


async def create_and_generate(
    url: str,
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

    ``name`` is validated BEFORE anything is written: it becomes the run's
    folder name, so a separator in it would write into another run's tree — or
    another instance's tree, whose slug is derivable from its URL.
    """
    name = validate_run_name(name)
    _paths(url).prompts_path.mkdir(parents=True, exist_ok=True)
    (_paths(url).prompts_path / f"{name}.yaml").write_text(yaml.dump({"prompt": prompt}), encoding="utf-8")
    _paths(url).runs_file.parent.mkdir(parents=True, exist_ok=True)
    _paths(url).runs_file.write_text(yaml.dump({"active_run": name}), encoding="utf-8")

    # Resolve the run folder directly from the name: resolving through the
    # the active-run pointer (load_active_path) is a TOCTOU race — two concurrent
    # generations would both read whatever pointer was written last and emit
    # into the WRONG run folder. The pointer stays written for CLI compatibility.
    run_path = _paths(url).runs_path / name
    run_path.mkdir(parents=True, exist_ok=True)
    prompt_snapshot = _paths(url).prompts_path / f"{name}.yaml"
    if prompt_snapshot.is_file():
        shutil.copy2(prompt_snapshot, run_path / prompt_snapshot.name)

    # Building the runtime LOGS IN: it logs into the controlled Uwazi instance, so
    # an unreachable instance, a bad UWAZI_URL or bad credentials fail here —
    # before the try below exists and before any manifest is written. Without the
    # log the operator saw only the toast and the terminal stayed silent.
    try:
        runtime = build_runtime(user=user, password=password, url=url)
    except Exception as exc:
        logger.exception("run {}: could not build the Uwazi runtime", name)
        raise GenerateError(
            f"could not connect to the Uwazi instance ({exc}). Check UWAZI_URL and the Uwazi credentials."
        ) from exc
    use_case = GenerateScriptUseCase(
        llm=runtime.llm,
        deps=runtime.deps,
        # The generated script's own LLM allowance. The generation conversation
        # is not charged against it, but the generator must plan within it (and
        # knows whether it may use llm_complete/llm_map at all). With no explicit
        # budget the planning pass falls back to the configured script caps, so
        # the prompt is told the same effective allowance.
        budget=budget or _script_caps_budget(),
    )

    final_prompt = validated_prompt or prompt
    try:
        script = await use_case.execute(final_prompt)
        run_path.mkdir(parents=True, exist_ok=True)
        emit_generated_script(script, run_path)
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
        plan_kind=script.plan_kind,
        plan_description=script.description,
        status=RunStatus.PLANNED,
        snapshot_dir=str(run_path),
    )
    runtime.backup_store.save_manifest(name, manifest)

    # The planning scan runs AUTOMATICALLY, unasked: the script cannot call the
    # LLM in this pass (it is bound no-LLM dry-run namespace), so nothing here
    # costs money — the review table's rows are simply built the moment the
    # script exists.
    try:
        await run_auto_scan(url, name, user, password, budget)
    except PredictionPassError as exc:
        # The plan contradicted its declared kind (or the script itself failed):
        # a run with no reviewable table is a FAILED generation, and saying so is
        # what enables the retry affordance instead of leaving a PLANNED run with
        # an empty table.
        manifest.status = RunStatus.GENERATION_FAILED
        manifest.error = str(exc)
        manifest.error_step = "plan"
        runtime.backup_store.save_manifest(name, manifest)
        raise GenerateError(str(exc)) from exc


async def run_auto_scan(
    url: str,
    run_id: str,
    user: str,
    password: str,
    budget: LlmBudget | None = None,
    script_override: str | None = None,
    plan_kind_override: str | None = None,
) -> ReconciliationSummary:
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
    summary = await generate_predictions(
        url,
        run_id,
        user,
        password,
        budget or _script_caps_budget(),
        None,
        script_override=script_override,
        plan_kind_override=plan_kind_override,
    )
    # The rows just recorded are Uwazi's CURRENT state: capture their editDates
    # as the drift baseline now, so a later manual edit is detectable. Failure
    # is non-fatal (the table is still built). Threaded: it is a paged
    # ``/api/v2/search`` per language, and on the event loop it froze the page.
    await asyncio.to_thread(refresh_baseline_after_run, url, run_id, user, password)
    return summary


async def generate_predictions(
    url: str,
    run_id: str,
    user: str,
    password: str,
    budget: LlmBudget,
    shared_ids: list[str] | None = None,
    script_override: str | None = None,
    plan_kind_override: str | None = None,
) -> ReconciliationSummary:
    """Run the planning pass over a run's script and persist its rows.

    A subset pass passes ``shared_ids``; None means the script discovers its own
    scope. The pass cannot mutate Uwazi (every write is a recorded no-op). The
    script's OWN llm_complete/llm_map calls ride the LLM runtime under the
    ledger built from ``budget`` — the caps the operator approved at task
    creation; a breach aborts the pass keeping earlier rows.

    ``script_override`` / ``plan_kind_override`` plan a REGENERATED script that is
    not committed yet: the caller (the chat's Run) passes the freshly generated
    text, and only persists it when this pass accepted it. Without them the run's
    committed ``script.py`` and manifest are used.

    Returns the reconciliation summary of the merge into the ONE live table.

    The run's declared ``plan_kind`` is enforced (see
    :func:`uwazi_admin_agent.domain.plan_intent.plan_intent_problem`): a
    ``changes`` plan that records no change operation fails the pass instead of
    replacing the table with display-only info rows.
    """
    runtime = build_runtime(user=user, password=password, url=url)
    script = script_override if script_override is not None else read_run_script(url, run_id)
    plan_kind = (
        plan_kind_override if plan_kind_override is not None else runtime.backup_store.load_manifest(run_id).plan_kind
    )
    use_case = GeneratePredictionsUseCase(
        entity_api=runtime.entity_api,
        entity_repository=runtime.entity_repository,
        file_repository=runtime.file_repository,
        segmentation_repository=runtime.segmentation_repository,
        url_fetcher=runtime.url_fetcher,
        suggestion_store=build_suggestion_store(url),
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
    await use_case.run(script, run_id=run_id, budget=budget, entity_scope=shared_ids, plan_kind=plan_kind)
    return use_case.last_reconciliation or ReconciliationSummary()


async def regenerate_from_chat(
    url: str,
    run_id: str,
    history: list[ChatMessage],
    user: str,
    password: str,
    budget: LlmBudget | None = None,
    on_stage: Callable[[str], None] | None = None,
) -> TableUpdateResult:
    """Regenerate the run's script from the whole chat history and reconcile the table.

    The chat's Run button. The steps, in order:

    1. fold the WHOLE chat into ONE prompt — the original prompt, the
       validation step's resolved clarifications, and every operator request
       with its own resolved clarifications, each under an explicit heading, so
       the generation LLM sees the complete context and can produce a NEW plan
       from it (``build_final_prompt_from_chat``);
    2. generate a FRESH script from that prompt with the CURRENT system prompt
       (no persisted-conversation replay: pydantic-ai only injects
       ``system_prompt=`` on an empty history, so replaying an old conversation
       silently ran the contract of the day it was captured);
    3. overwrite the run's ONE script file (``script.py``);
    4. update the manifest — including ``validated_prompt`` (the full context the
       script was generated from), ``plan_kind`` and ``plan_description``;
    5. re-scan: the planning pass runs the NEW script and MERGES its rows into
       the ONE live table (rows the script still produces keep their identity
       and status, changed rows go back to pending, rows it no longer produces
       become obsolete). A plan whose kind contradicts its records fails the
       pass and leaves the previous table untouched.

    ``on_stage`` (optional) is called with a human-readable stage description
    before each long step, so the caller can show progress instead of a silent
    modal. It must not block: the driver only forwards text to the UI.

    Returns the reconciliation summary. Errors propagate: the caller shows them.
    """
    runtime = build_runtime(user=user, password=password, url=url)
    manifest = runtime.backup_store.load_manifest(run_id)
    run_path = _paths(url).runs_path / run_id
    final_prompt = build_final_prompt_from_chat(history)
    use_case = GenerateScriptUseCase(
        llm=runtime.llm,
        deps=runtime.deps,
        # The budget that will govern this regenerated script's planning pass;
        # the generator plans LLM usage against it (see the script-budget
        # section of the system prompt). No explicit budget falls back to the
        # configured script caps, exactly like the planning pass.
        budget=budget or _script_caps_budget(),
    )
    if on_stage is not None:
        on_stage("Regenerating the script from the whole prompt context…")
    try:
        script = await use_case.execute(final_prompt)
    except Exception as exc:  # noqa: BLE001 — surface as a generation error
        raise GenerateError(str(exc)) from exc

    if on_stage is not None:
        on_stage("Recomputing the table from the new script…")
    # PLAN FIRST, COMMIT SECOND. The regenerated script is planned straight from
    # its own source, BEFORE it replaces the run's committed script/manifest: a
    # plan that contradicts its declared kind (or crashes) must leave the
    # previous table AND the previous script exactly as the operator last
    # reviewed them. Only an accepted plan is persisted.
    try:
        summary = await run_auto_scan(
            url,
            run_id,
            user,
            password,
            budget,
            script_override=script.python_code,
            plan_kind_override=script.plan_kind,
        )
    except PredictionPassError as exc:
        # The regenerated plan has NOTHING runnable (or its kind is contradicted):
        # the run keeps the script and table the operator last reviewed, and the
        # status says this attempt produced nothing — the chat shows the message.
        manifest.status = RunStatus.GENERATION_FAILED
        manifest.error = str(exc)
        manifest.error_step = "plan"
        runtime.backup_store.save_manifest(run_id, manifest)
        raise GenerateError(str(exc)) from exc

    if on_stage is not None:
        on_stage("Saving the new script and prompt…")
    emit_generated_script(script, run_path)
    manifest.script = script.python_code
    manifest.plan_kind = script.plan_kind
    manifest.plan_description = script.description
    manifest.status = RunStatus.PLANNED
    manifest.error = None
    manifest.error_step = None
    manifest.validated_prompt = final_prompt
    runtime.backup_store.save_manifest(run_id, manifest)
    logger.info(
        "regenerated from chat run={} prompt_chars={} plan_kind={}",
        run_id,
        len(final_prompt),
        script.plan_kind,
    )
    return TableUpdateResult(
        run_id=run_id,
        changed_in_uwazi=None,
        reconciliation=summary,
        outcome=_plan_outcome_of(url, run_id),
    )


def _plan_outcome_of(url: str, run_id: str) -> PlanOutcome | None:
    """The last pass's plan outcome as the table presents it (None = unknown).

    Read back from the persisted rows rather than remembered in memory: the
    regeneration path plans a script it has not committed yet, and the caller
    reports what the store actually holds. ``None`` when the run has no rows at
    all (nothing was ever planned).
    """
    store = build_suggestion_store(url)
    rows, total = store.query(run_id, SuggestionFilter(include_obsolete=True, limit=1))
    if total == 0 and not rows:
        return None
    pass_row = store.get_pass(run_id)
    if pass_row is None:
        return None
    rows_all, _ = store.query(run_id, SuggestionFilter(include_obsolete=True, limit=100000))
    info_parents = sum(1 for row in rows_all if row.op == INFO_OP and row.detail_key is None)
    change_rows = sum(1 for row in rows_all if row.op not in DISPLAY_ONLY_OPS)
    has_up_to_date_note = any(
        row.op == INFO_OP
        and (
            str(row.proposed_value or "").startswith("Nothing to run:")
            or str(row.entity_title or "").startswith("Nothing to run:")
        )
        for row in rows_all
    )
    matched_rows = sum(1 for row in rows_all if row.op == MATCHED_OP)
    return PlanOutcome(
        change_rows=change_rows,
        unchanged_rows=matched_rows if matched_rows else (1 if has_up_to_date_note else 0),
        info_rows=info_parents,
        file_rows=0,
        matched_rows=matched_rows,
        skipped_rows=sum(1 for row in rows_all if row.op == SKIPPED_OP),
    )
