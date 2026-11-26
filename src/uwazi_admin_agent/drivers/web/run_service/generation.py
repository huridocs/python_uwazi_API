"""Web driver module split out of the original ``run_service`` (behavior preserved)."""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import yaml
from loguru import logger

from uwazi_admin_agent.adapters.pydantic_ai_runtime import PydanticAiLlmRuntime
from uwazi_admin_agent.adapters.script_emitter import emit_generated_script
from uwazi_admin_agent.configuration import (
    DUMMY_LANGUAGE,
)
from uwazi_admin_agent.domain.generated_script import GeneratedScript
from uwazi_admin_agent.domain.llm_budget import LlmBudget
from uwazi_admin_agent.domain.manifest import MigrationManifest, RunStatus
from uwazi_admin_agent.domain.plan_intent import PlanOutcome
from uwazi_admin_agent.domain.prompt_chat import ChatMessage, build_final_prompt_from_chat
from uwazi_admin_agent.domain.prompt_revision import prompt_with_previous_script, script_repair_request
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
from uwazi_admin_agent.use_cases.generation_stages import (
    STAGE_PLANNING,
    STAGE_REPAIR,
    STAGE_SAVING,
)

from .drift import refresh_baseline_after_run
from .models import GenerateError, TableUpdateResult
from .wiring import _paths, _script_caps_budget, build_suggestion_store, read_run_script


async def _repair_failed_plan(
    use_case: GenerateScriptUseCase,
    *,
    prompt: str,
    failed_script: str,
    error: str,
    plan_kind: str | None,
    on_stage: Callable[[str], None] | None,
) -> GeneratedScript | None:
    """ONE bounded repair round for a freshly generated script that crashed.

    The traceback is re-opened as the authoring turn's user prompt (see
    :func:`script_repair_request`): the SAME use case — same model, tools and
    budget the first generation ran with — outputs a corrected full script.
    Returns it, or ``None`` when the repair turn itself fails. The caller
    re-plans the corrected source with ``script_override``; a repaired script
    that crashes again does NOT loop — the caller keeps the original crash as
    the run's failure and the operator's Retry stays the escape hatch.
    """
    if on_stage is not None:
        on_stage(STAGE_REPAIR)
    repair_prompt = script_repair_request(prompt, failed_script, error, plan_kind)
    try:
        return await use_case.execute(repair_prompt, on_stage=on_stage)
    except Exception as exc:  # noqa: BLE001 — a failed repair keeps the original crash
        logger.warning("script repair round failed: {}", exc)
        return None


def _config_dict(validation_config: Any | None) -> dict[str, Any] | None:
    """The wizard's test-set recipe as plain data for the manifest (pure).

    A plain dict keeps ``domain.manifest`` free of an import edge into the
    validation scoring chain (``domain.metadata_diff`` → ``manifest``).
    """
    if validation_config is None:
        return None
    dump = getattr(validation_config, "model_dump", None)
    return dump(mode="json") if callable(dump) else None


async def create_and_generate(
    url: str,
    name: str,
    prompt: str,
    user: str,
    password: str,
    validated_prompt: str | None = None,
    budget: LlmBudget | None = None,
    on_stage: Callable[[str], None] | None = None,
    validation_config: Any | None = None,
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

    ``on_stage`` (optional) receives a short operator-facing sentence as the
    work moves (reading the schema, authoring the extractor, running the
    benchmark, writing the script, planning the table). The web driver turns it
    into the run row's live status caption.
    """
    name = validate_run_name(name)
    # A run folder that already holds a manifest IS a task the operator can see
    # and review. Generating into it would overwrite that run's script and
    # manifest without adding anything to the table — which is exactly how a
    # repeated Duplicate looked like "the second task was never created". The
    # retry path deletes the failed run before calling here, so it is unaffected.
    existing = _paths(url).runs_path / name / "manifest.json"
    if existing.exists():
        raise GenerateError(f"A run named {name!r} already exists — pick another name.")
    # The holdout is resolved and frozen BEFORE the generation turn: sampling
    # reads the instance once, and from the moment the set exists every
    # authoring read through build_runtime excludes those entities. AFTER the
    # existence check: a refused duplicate must not overwrite another run's set.
    #
    # A set that could NOT be built is a normal outcome of a test-set recipe with
    # no matching entities (the operator may even WANT a small scope): it is
    # logged for support and otherwise silent. The run is usable and simply runs
    # without a performance score — the empty validation card says that on its
    # own. Warning about it above the table told the operator something is broken
    # when nothing is.
    if validation_config is not None:
        try:
            from .validation import build_validation_set

            frozen = await build_validation_set(url, name, validation_config, user, password)
            if frozen is None:
                logger.info(
                    "no validation set built for run {} (property {!r}): running without a score",
                    name,
                    validation_config.property_name,
                )
        except Exception as exc:  # noqa: BLE001 — never lose the run over an optional set
            logger.warning("validation sampling failed for run {}: {}", name, exc)
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
        from .validation import holdout_ids

        runtime = build_runtime(user=user, password=password, url=url, holdout_ids=holdout_ids(url, name) or None)
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
        script = await use_case.execute(final_prompt, on_stage=on_stage)
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
            llm_budget=budget,
            validation_config=_config_dict(validation_config),
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
        llm_budget=budget,
        validation_config=_config_dict(validation_config),
    )
    runtime.backup_store.save_manifest(name, manifest)

    # The planning scan runs AUTOMATICALLY, unasked: the script cannot call the
    # LLM in this pass (it is bound no-LLM dry-run namespace), so nothing here
    # costs money — the review table's rows are simply built the moment the
    # script exists.
    if on_stage is not None:
        on_stage(STAGE_PLANNING)
    try:
        await run_auto_scan(url, name, user, password, budget)
    except PredictionPassError as exc:
        # 1. SELF-REPAIR, once: a script CRASH during the FIRST plan pass is a
        #    model typo (the wrong helper name, an attribute the result object
        #    does not have). The traceback is exactly the evidence the authoring
        #    agent needs, so the same generation turn is re-opened with the
        #    failed source and the crash text. An intent-gate REFUSAL is not
        #    repaired: that is the design answering the prompt, and re-authoring
        #    it would not know what to change.
        repaired = (
            None
            if exc.refused
            else await _repair_failed_plan(
                use_case,
                prompt=final_prompt,
                failed_script=script.python_code,
                error=str(exc),
                plan_kind=manifest.plan_kind,
                on_stage=on_stage,
            )
        )
        # 2. Re-plan with the corrected source BEFORE it replaces anything: the
        #    SAME declared plan kind, and a pass that crashes again (the repair
        #    could not fix the typo) keeps the original crash as this run's
        #    failure — no repair loop, Retry stays the escape hatch.
        replanned: ReconciliationSummary | None = None
        if repaired is not None:
            if on_stage is not None:
                on_stage(STAGE_PLANNING)
            try:
                replanned = await run_auto_scan(
                    url,
                    name,
                    user,
                    password,
                    budget,
                    script_override=repaired.python_code,
                    plan_kind_override=manifest.plan_kind,
                )
            except PredictionPassError as repair_error:
                logger.warning("run {}: the repaired script also failed to plan: {}", name, repair_error)
        if replanned is None:
            manifest.status = RunStatus.GENERATION_FAILED
            manifest.error = str(exc)
            manifest.error_step = "plan"
            runtime.backup_store.save_manifest(name, manifest)
            raise GenerateError(str(exc)) from exc
        # 3. The repaired script planned cleanly under the same declared kind:
        #    persist it — it is now the run's committed script, the one that
        #    produced the review table the operator is about to see.
        logger.info(
            "run {}: the first plan pass crashed ({}); the repaired script planned cleanly",
            name,
            str(exc).splitlines()[0][:120],
        )
        emit_generated_script(repaired, run_path)
        manifest.script = repaired.python_code
        manifest.plan_description = repaired.description
        runtime.backup_store.save_manifest(name, manifest)

    # The script is committed and planned: score it against the holdout (if the
    # run has one). A scoring failure must never lose a usable run.
    try:
        from .validation import evaluate_run

        await evaluate_run(url, name, user, password)
    except Exception as exc:  # noqa: BLE001
        logger.warning("validation scoring failed for run {}: {}", name, exc)


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
    rows go back to pending, rows it no longer produces are deleted — except an
    APPLIED write, kept as history).
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
    from .validation import hidden_property as hidden_measured_property
    from .validation import holdout_ids

    holdout = holdout_ids(url, run_id)
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
    await use_case.run(
        script,
        run_id=run_id,
        budget=budget,
        entity_scope=shared_ids,
        plan_kind=plan_kind,
        # The production pass never reads the run's holdout entities: the review
        # table must not propose writes for them. A subset pass narrows further;
        # the store protects the validation rows from being retired meanwhile.
        exclude_scope=holdout or None,
        # The property the run's score measures is not readable either: its
        # stored value must never become the proposal it is compared against.
        hidden_property=hidden_measured_property(url, run_id),
    )
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
       are deleted). A plan whose kind contradicts its records fails the
       pass and leaves the previous table untouched.

    The run's CURRENT ``script.py`` is ALWAYS sent alongside the folded prompt
    when one exists. A chat Run adds information to a script whose review table
    the operator has already seen; re-deriving it from the prompt alone made the
    result worse (the original extraction logic was lost) even though the new
    prompt carried MORE information. The generation turn therefore revises the
    known script with the SAME authoring tools the first turn had (schema
    reads, source sampling, ``author_html_extractor``, ``benchmark_extractor``).
    That attachment is generation-only: ``validated_prompt`` still records the
    operator's context, and the manifest's ``script`` field records the source.

    ``on_stage`` (optional) is called with a human-readable stage description
    before each long step, so the caller can show progress instead of a silent
    modal. It must not block: the driver only forwards text to the UI.

    Returns the reconciliation summary. Errors propagate: the caller shows them.
    """
    runtime = build_runtime(user=user, password=password, url=url)
    manifest = runtime.backup_store.load_manifest(run_id)
    run_path = _paths(url).runs_path / run_id
    final_prompt = build_final_prompt_from_chat(history)
    try:
        previous_script = read_run_script(url, run_id)
    except OSError:
        # A run with no committed source has nothing to revise: the folded
        # prompt is enough, and the generation continues normally.
        logger.info("chat regeneration for run={} has no committed script", run_id)
        previous_script = ""
    prompt_for_generation = prompt_with_previous_script(final_prompt, previous_script)
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
        on_stage("LLM is regenerating the script from the whole prompt context…")
    try:
        script = await use_case.execute(prompt_for_generation, on_stage=on_stage)
    except Exception as exc:  # noqa: BLE001 — surface as a generation error
        raise GenerateError(str(exc)) from exc

    if on_stage is not None:
        on_stage(STAGE_PLANNING)
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
        on_stage(STAGE_SAVING)
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
    # The committed script changed: the stored score describes the OLD one. The
    # new script is scored against the same frozen holdout; a scoring failure
    # never loses a usable table.
    try:
        from .validation import evaluate_run

        await evaluate_run(url, run_id, user, password)
    except Exception as exc:  # noqa: BLE001
        logger.warning("validation scoring after regeneration failed for run {}: {}", run_id, exc)
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
    rows, total = store.query(run_id, SuggestionFilter(limit=1))
    if total == 0 and not rows:
        return None
    pass_row = store.get_pass(run_id)
    if pass_row is None:
        return None
    rows_all, _ = store.query(run_id, SuggestionFilter(limit=100000))
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
