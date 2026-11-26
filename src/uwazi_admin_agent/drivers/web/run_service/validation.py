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

from uwazi_admin_agent.adapters.validation_store import ValidationStore
from uwazi_admin_agent.domain.suggestion_scoring import value_in_language
from uwazi_admin_agent.domain.validation import (
    ValidationConfig,
    ValidationScore,
    ValidationSet,
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
    )
    score = await use_case.evaluate(script, validation, now=datetime.now(timezone.utc))
    store.save_score(score)
    rows = use_case.last_rows
    if rows is not None:
        build_suggestion_store(url).replace_validation_rows(run_id, rows)
    return score


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

    The rows copied to the production table keep their proposal and become
    PENDING, so the operator can review and Run them. The set is shrunk and its
    version bumped: the stored score is stale from then on.

    Returns the production row ids created/updated.
    """
    store = validation_store(url, run_id)
    validation = store.load_set()
    if validation is None or shared_id not in validation.shared_ids:
        return []
    suggestion_store = build_suggestion_store(url)
    rows, _total = suggestion_store.query(run_id, _validation_filter(shared_id))
    ids = [row.id for row in rows if row.id is not None]
    if ids:
        suggestion_store.promote_to_regular(run_id, shared_id)
    store.save_set(validation.remove_entity(shared_id))
    logger.info("promoted validation entity run={} shared_id={} rows={}", run_id, shared_id, len(ids))
    return ids


def remove_validation_row(url: str, run_id: str, shared_id: str) -> None:
    """Drop ONE entity from the validation set without promoting its rows."""
    store = validation_store(url, run_id)
    validation = store.load_set()
    if validation is None or shared_id not in validation.shared_ids:
        return
    build_suggestion_store(url).drop_validation_rows(run_id, shared_id)
    store.save_set(validation.remove_entity(shared_id))


async def add_validation_sample(url: str, run_id: str, shared_id: str, user: str, password: str) -> ValidationSet | None:
    """Add one entity (and its current value) to the run's set.

    Adding an entity the generation turn already read contaminates the set: the
    score stops being an unseen estimate and the UI says so.
    """
    store = validation_store(url, run_id)
    validation = store.load_set()
    if validation is None:
        return None

    runtime = build_runtime(user=user, password=password, url=url)
    language = "en"
    raw = await runtime.entity_repository.get_raw_by_shared_id(shared_id, language)
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    value = (
        raw.get("title")
        if validation.property_name == "_title"
        else value_in_language(metadata.get(validation.property_name), language)
    )
    updated = validation.add_entity(shared_id, language, value)
    updated = updated.model_copy(update={"contaminated_at": datetime.now(timezone.utc)})
    store.save_set(updated)
    return updated


def _validation_filter(shared_id: str):
    from uwazi_admin_agent.domain.suggestion import SuggestionFilter

    return SuggestionFilter(shared_ids=[shared_id], is_validation=True, limit=10000)


__all__ = [
    "add_validation_sample",
    "build_validation_set",
    "evaluate_run",
    "entity_is_validation",
    "holdout_ids",
    "promote_validation_row",
    "remove_validation_row",
    "validation_store",
    "validation_summary",
]
