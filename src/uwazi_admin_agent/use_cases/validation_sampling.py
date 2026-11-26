"""Resolve a wizard's validation choice into a frozen :class:`ValidationSet`.

Automatic sampling reads the instance ONCE, at run creation (before the
generation turn), from the templates the task targets:

1. every entity of the selected templates is listed (bounded by the requested
   size so a huge instance is not paged in full);
2. entities whose MEASURED property already holds a value are kept — they are
   the ones that can be scored automatically;
3. the first ``sample_size`` in the instance's own order are frozen with that
   value.

Custom sampling runs the LLM-authored SELECTOR the operator's description
produced (see :mod:`uwazi_admin_agent.domain.validation_selector`): a small
read-only snippet that picks the entities by property filter, date window or
explicit shared ids — things a free-text search cannot express. Either way the
entities must already carry the measured value; when fewer are found than
asked, the set is whatever was found — never padded, never invented. An empty
sample is NOT a validation set: the caller reports that and the run is created
without one.

This is a run-creation read, deliberately outside the holdout wrappers (the set
does not exist yet). It is the LAST read that touches those entities before
they are frozen.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from uwazi_admin_agent.domain.suggestion_scoring import value_in_language
from uwazi_admin_agent.domain.validation import (
    VALIDATION_AUTOMATIC,
    VALIDATION_CUSTOM,
    VALIDATION_NONE,
    ValidationConfig,
    ValidationSet,
    label_key,
    validation_property_name,
)
from uwazi_admin_agent.domain.validation_selector import selector_entity_dicts, selector_shared_ids
from uwazi_admin_agent.use_cases.script_exec_namespace import (
    build_validation_selector_namespace,
    run_script_sync,
)
from uwazi_agent.ports.entity_api_port import EntityApiPort

# How many extra entities to inspect to still find ``sample_size`` carrying the
# measured value: the first N of a template may all be empty.
_SCAN_MULTIPLIER: int = 20


def _property_value(entity_any: Any, property_name: str, language: str) -> Any:
    """The measured property's value on one entity, in one language (pure)."""
    if isinstance(entity_any, dict):
        if property_name == "_title":
            return entity_any.get("title")
        metadata = entity_any.get("metadata") or {}
        return value_in_language(metadata.get(property_name) if isinstance(metadata, dict) else None, language)
    metadata = getattr(entity_any, "metadata", None) or {}
    if property_name == "_title":
        return getattr(entity_any, "title", None)
    return value_in_language(metadata.get(property_name), language)


def _has_value(value: Any) -> bool:
    """Whether a value can be scored against (pure)."""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict)):
        return bool(value)
    return True


def _run_selector(entity_api: EntityApiPort, code: str) -> Any:
    """Execute the selector snippet on its own event loop (worker thread).

    Mirrors the planning pass: the bound read helpers are synchronous and drive
    the port through ``loop.run_until_complete``, so the snippet needs a loop of
    its own. The namespace holds READS only — a selector can never write, and it
    cannot ask the LLM.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        namespace = build_validation_selector_namespace(entity_api, loop)
        _result, error = run_script_sync(code, namespace)
    finally:
        loop.close()
    if error is not None:
        raise ValueError(f"the test-set selector failed: {error.strip().splitlines()[0]}")
    return namespace.get("result")


def _selected(
    selected: list[str],
    entities_by_id: dict[str, Any],
    property_name: str,
    language: str,
    wanted: int,
) -> tuple[list[str], dict[str, Any]]:
    """Keep the selected entities that carry a measurable value (pure)."""
    shared_ids: list[str] = []
    labels: dict[str, Any] = {}
    for shared_id in selected:
        if len(shared_ids) >= wanted:
            break
        entity = entities_by_id.get(shared_id)
        if entity is None:
            continue
        value = _property_value(entity, property_name, language)
        if not _has_value(value):
            continue
        shared_ids.append(shared_id)
        labels[label_key(shared_id, language)] = value
    return shared_ids, labels


async def resolve_validation_set(
    entity_api: EntityApiPort,
    run_id: str,
    config: ValidationConfig,
    now: datetime | None = None,
) -> ValidationSet | None:
    """Freeze a sample for one run, or None when nothing judgeable was found.

    ``config.mode`` decides the source: ``automatic`` scans the selected
    templates; ``custom`` runs the operator's selector snippet. Both keep only
    entities carrying a value for the measured property. ``none`` returns None
    without a read.
    """
    if config.mode == VALIDATION_NONE or not config.property_name.strip():
        return None
    property_name = validation_property_name(config.property_name)
    wanted = max(1, int(config.sample_size))
    limit = wanted * _SCAN_MULTIPLIER

    candidates: list[Any] = []
    selection_code = (config.selector_code or "").strip()
    if config.mode == VALIDATION_CUSTOM and selection_code:
        raw = await asyncio.to_thread(_run_selector, entity_api, selection_code)
        selected = selector_shared_ids(raw)
        entities_by_id: dict[str, Any] = dict(selector_entity_dicts(raw))
        missing = [shared_id for shared_id in selected if shared_id not in entities_by_id]
        if missing:
            fetched = await entity_api.get_entities_by_shared_ids(missing, config.language, limit=len(missing))
            for entity in fetched:
                entities_by_id.setdefault(str(getattr(entity, "shared_id", "") or ""), entity)
        shared_ids, labels = _selected(selected, entities_by_id, property_name, config.language, wanted)
    else:
        templates = config.template_names or [None]
        for template_name in templates:
            if template_name is None:
                continue
            found = await entity_api.get_entities_by_template(template_name, config.language, limit)
            candidates.extend(list(found._all_entities) or list(found.examples))
        shared_ids, labels = _selected_from_entities(candidates, property_name, config.language, wanted)

    if not shared_ids:
        logger.info("validation: no judgeable sample for property {} in {}", property_name, config.template_names)
        return None

    return ValidationSet(
        run_id=run_id,
        property_name=property_name,
        property_type=config.property_type,
        template_names=list(config.template_names),
        shared_ids=shared_ids,
        labels=labels,
        mode=VALIDATION_AUTOMATIC if config.mode == VALIDATION_AUTOMATIC else VALIDATION_CUSTOM,
        input_properties=sorted({str(name).strip() for name in config.input_properties if str(name).strip()}),
        selector_explanation=(config.selector_explanation or "").strip() if config.mode == VALIDATION_CUSTOM else "",
        created_at=now or datetime.now(timezone.utc),
    )


def _selected_from_entities(
    entities: list[Any],
    property_name: str,
    language: str,
    wanted: int,
) -> tuple[list[str], dict[str, Any]]:
    """Keep the first ``wanted`` entities carrying a measurable value (pure)."""
    by_id: dict[str, Any] = {}
    order: list[str] = []
    for entity in entities:
        shared_id = str(getattr(entity, "shared_id", "") or "")
        if shared_id and shared_id not in by_id:
            by_id[shared_id] = entity
            order.append(shared_id)
    return _selected(order, by_id, property_name, language, wanted)


__all__ = ["resolve_validation_set"]
