"""Validation-service glue for the web layer: sample, score, promote.

The review page and the wizard share these functions; they own the runtime
wiring (the holdout must be passed to ``build_runtime`` so generation cannot
read the sampled entities) and the small file store beside the manifest.

The rule this module keeps intact: validation rows are evidence, never work.
The apply guard lives in ``apply_suggestions_use_case/payloads.py``; the only
way a validation entity becomes work is an explicit PROMOTION, which removes it
from the set and relabels its rows.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from loguru import logger

from uwazi_admin_agent.adapters.pydantic_ai_runtime import PydanticAiLlmRuntime
from uwazi_admin_agent.adapters.validation_store import ValidationStore
from uwazi_admin_agent.domain.suggestion import SuggestionFilter
from uwazi_admin_agent.domain.suggestion_scoring import value_in_language
from uwazi_admin_agent.domain.validation import (
    ValidationConfig,
    ValidationScore,
    ValidationSet,
    split_label_key,
    validation_state,
)
from uwazi_admin_agent.drivers.runtime import build_runtime
from uwazi_admin_agent.use_cases.evaluate_validation_use_case import ValidationEvaluationUseCase
from uwazi_admin_agent.use_cases.generate_validation_selector_use_case import (
    GenerateValidationSelectorUseCase,
)
from uwazi_admin_agent.use_cases.validation_sampling import resolve_validation_set

from .wiring import _paths, build_suggestion_store, read_run_script


def validation_store(url: str, run_id: str) -> ValidationStore:
    """The run's validation files (``validation.yaml`` + ``validation_score.json``)."""
    return ValidationStore(_paths(url).runs_path / run_id)


def _run_budget(url: str, run_id: str):
    """The run's approved script-LLM budget (the pass row's), or None.

    The evaluation pass must not invent an allowance: it runs the SAME script
    the planning pass ran, so it runs under the SAME ledger contract. None means
    the evaluation keeps its deny-by-default ceiling (a run with no approved
    allowance may not spend on its own score).
    """
    pass_row = build_suggestion_store(url).get_pass(run_id)
    return pass_row.budget if pass_row is not None else None


def hidden_property(url: str, run_id: str) -> str | None:
    """The measured property whose stored value the run's script must not read.

    Empty when the run has no test set, or when the operator declared that
    property as an INPUT (a source metadata property, or the target itself for
    the "only where it is empty" scope).
    """
    from uwazi_admin_agent.use_cases.script_exec_namespace import hidden_measured_property

    validation = validation_store(url, run_id).load_set()
    if validation is None:
        return None
    return hidden_measured_property(validation.property_name, validation.input_properties)


def holdout_ids(url: str, run_id: str) -> set[str]:
    """The sharedIds generation must not read for this run (empty when none)."""
    validation = validation_store(url, run_id).load_set()
    return set(validation.shared_ids) if validation is not None else set()


async def build_validation_set(
    url: str,
    run_id: str,
    config: ValidationConfig,
    user: str,
    password: str,
) -> ValidationSet | None:
    """Resolve and persist the run's validation set (None when nothing judgeable).

    Runs BEFORE generation: the sampling read is the last time these entities are
    touched by the authoring side, and the set is written to disk before the
    generation turn starts so ``build_runtime(holdout_ids=...)`` can hide them.

    A CUSTOM config carries only the operator's description of how the sample
    should be chosen. This is where the LLM turns it into the read-only selector
    (``selector_code``): the wizard deliberately does not call the model while
    the operator fills the form, so the whole task generation is one flow.

    A coroutine, not a sync function: its caller (``create_and_generate``) already
    runs inside an event loop. The old ``asyncio.run`` here raised "cannot be
    called from a running event loop", so the set was silently never built.
    """
    # RAW ports on purpose: this read DEFINES the set, so it must see the
    # entities the authoring runtime will later hide.
    runtime = build_runtime(user=user, password=password, url=url)
    if config.mode == "custom" and not config.selector_code.strip():
        selector = await GenerateValidationSelectorUseCase(llm=runtime.llm, deps=runtime.deps).execute(
            config.selector_explanation or "",
            template_names=list(config.template_names),
            property_name=config.property_name,
            sample_size=max(1, int(config.sample_size)),
            language=config.language,
        )
        config = config.model_copy(update={"selector_code": selector.python_code})
        logger.info(
            "validation selector authored run={}: {} entity/entities requested",
            run_id,
            config.sample_size,
        )
    validation = await resolve_validation_set(
        runtime.entity_api,
        run_id,
        config,
        now=datetime.now(timezone.utc),
    )
    if validation is None:
        return None
    store = validation_store(url, run_id)
    store.save_set(validation)
    logger.info(
        "validation set frozen run={} entities={} property={}",
        run_id,
        len(validation.shared_ids),
        validation.property_name,
    )
    return validation


async def evaluate_run(url: str, run_id: str, user: str, password: str) -> ValidationScore | None:
    """Score the run's committed script against its set, and REFRESH its rows.

    Called after a generation/regeneration committed a script. The scoring pass
    runs the script over the held-out entities; its recorded writes become the
    table's validation rows (marked ``is_validation``), so the operator sees
    exactly which held-out entities the script fails on. Its labels are the
    frozen ones, never the live values.
    """
    store = validation_store(url, run_id)
    validation = store.load_set()
    if validation is None:
        return None
    script = read_run_script(url, run_id)
    # RAW ports on purpose: the evaluation pass must read the holdout entities.
    runtime = build_runtime(user=user, password=password, url=url)
    use_case = ValidationEvaluationUseCase(
        entity_api=runtime.entity_api,
        entity_repository=runtime.entity_repository,
        file_repository=runtime.file_repository,
        segmentation_repository=runtime.segmentation_repository,
        url_fetcher=runtime.url_fetcher,
        translation_port=runtime.translation_port,
        default_language="en",
        # The script's own llm_complete/llm_map calls, bound exactly as the
        # planning pass binds them — under the run's APPROVED budget, so the
        # measured pass is the pass the operator will run. Without this every
        # LLM-driven row recorded ``skipped`` and the score was 0.
        llm_runtime=PydanticAiLlmRuntime(runtime.llm),
        llm_cache=runtime.file_cache,
        llm_budget=_run_budget(url, run_id),
    )
    score = await use_case.evaluate(
        script,
        validation,
        now=datetime.now(timezone.utc),
        hidden_property=hidden_property(url, run_id),
    )
    store.save_score(score)
    rows = use_case.last_rows
    if rows is not None:
        build_suggestion_store(url).replace_validation_rows(run_id, rows)
    return score


async def rescore_run(url: str, run_id: str, user: str, password: str) -> Any:
    """Re-score a run after its validation set changed (set edit or promotion).

    The set's version moved, so the stored score is stale/absent by construction;
    this recomputes it from the committed script and refreshes the holdout rows.
    Returns the new score, or None when the run has no set or nothing to score.
    """
    return await evaluate_run(url, run_id, user, password)


def entity_is_validation(url: str, run_id: str, shared_id: str) -> bool:
    """Whether one entity is in the run's holdout set (the entity window's toggle)."""
    validation = validation_store(url, run_id).load_set()
    return validation is not None and shared_id in validation.shared_ids


def validation_summary(url: str, run_id: str) -> dict[str, Any]:
    """What the review page's score card reads (set + latest score + state)."""
    store = validation_store(url, run_id)
    validation = store.load_set()
    score = store.load_score()
    if validation is None:
        return {"state": "none", "set": None, "score": None}
    return {
        "state": validation_state(validation, score),
        "set": validation,
        "score": score,
    }


def promote_validation_row(url: str, run_id: str, shared_id: str) -> list[int]:
    """Move ONE entity out of the validation set and make its rows regular work.

    The entity's rows keep their proposal and become PENDING, so the operator can
    review and Run them — BOTH its validation rows and its regular rows of the
    measured property. Promoting only the validation rows left the entity with a
    runnable row under a ``validate`` badge (the production row the table derives
    that badge from), so the row still read as evidence and Run addressed a value
    the pre-flight refused. The regular rows also take the FROZEN label as their
    current value, so a promoted entity reads like any other reviewed row.

    The set is shrunk and its version bumped: the stored score is stale from then
    on, and the caller re-scores it (see ``rescore_run``).

    Returns the ids of the rows the promotion touched.
    """
    store = validation_store(url, run_id)
    validation = store.load_set()
    if validation is None or shared_id not in validation.shared_ids:
        return []
    suggestion_store = build_suggestion_store(url)
    label = validation.label_for(shared_id, None)
    if label is None and validation.is_per_language():
        label = next(
            (value for key, value in validation.labels.items() if split_label_key(key)[0] == shared_id),
            None,
        )
    touched = suggestion_store.promote_to_regular(
        run_id,
        shared_id,
        label_property=validation.property_name,
        label=label,
    )
    removed = validation.remove_entity(shared_id)
    store.save_set(removed)
    # The old score described a set that no longer exists: drop it rather than
    # leave a number beside the new (unscored) set. The caller re-scores.
    store.clear_score()
    rows, _total = suggestion_store.query(run_id, SuggestionFilter(shared_ids=[shared_id], limit=10000))
    ids = [row.id for row in rows if row.id is not None]
    logger.info("promoted validation entity run={} shared_id={} rows={}", run_id, shared_id, touched)
    return ids


def remove_validation_row(url: str, run_id: str, shared_id: str) -> None:
    """Drop ONE entity from the validation set without promoting its rows."""
    store = validation_store(url, run_id)
    validation = store.load_set()
    if validation is None or shared_id not in validation.shared_ids:
        return
    build_suggestion_store(url).drop_validation_rows(run_id, shared_id)
    store.save_set(validation.remove_entity(shared_id))
    store.clear_score()


def add_validation_sample(url: str, run_id: str, shared_id: str, label: Any = None) -> ValidationSet | None:
    """Add one entity to the run's set AND mark its rows as evidence.

    Both halves are required for the operator to SEE the change:

    - ``validation.yaml`` gains the entity, with the label the score measures
      frozen at the moment of adding;
    - the entity's existing rows become validation rows, so the table's operation
      reads ``validate`` and its Run is withdrawn. Writing only the YAML left the
      rows looking like ordinary work, which is why the button looked dead.

    Adding an entity the generation turn already read contaminates the set: the
    score stops being an unseen estimate and the UI says so.
    """
    store = validation_store(url, run_id)
    validation = store.load_set()
    if validation is None:
        return None
    language = "en" if validation.is_per_language() else None
    updated = validation.add_entity(shared_id, language, label)
    updated = updated.model_copy(update={"contaminated_at": datetime.now(timezone.utc)})
    build_suggestion_store(url).mark_validation_rows(
        run_id,
        shared_id,
        label_for_language=lambda _language: label,
    )
    store.save_set(updated)
    # The stored score described the old set: clear it so the card says
    # "not scored yet" until the caller re-scores.
    store.clear_score()
    return updated


async def read_validation_label(url: str, run_id: str, shared_id: str, user: str, password: str) -> Any:
    """The value the score would measure for one entity, read live from Uwazi.

    The label is read ONCE, when the operator adds the entity, and then frozen
    with the set: the score must compare against the value the entity held when
    it entered the holdout, never against a later edit (or the run's own write).
    None means the entity holds no value — still a real measurement (a missing
    prediction), so the entity is recorded either way.
    """
    validation = validation_store(url, run_id).load_set()
    if validation is None:
        return None
    runtime = build_runtime(user=user, password=password, url=url)
    language = "en"
    raw = await runtime.entity_repository.get_raw_by_shared_id(shared_id, language)
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    if validation.property_name == "_title":
        return raw.get("title")
    return value_in_language(metadata.get(validation.property_name), language)


def _validation_filter(shared_id: str) -> SuggestionFilter:
    return SuggestionFilter(shared_ids=[shared_id], is_validation=True, limit=10000)


__all__ = [
    "add_validation_sample",
    "build_validation_set",
    "evaluate_run",
    "entity_is_validation",
    "hidden_property",
    "holdout_ids",
    "rescore_run",
    "promote_validation_row",
    "read_validation_label",
    "remove_validation_row",
    "validation_store",
    "validation_summary",
]
