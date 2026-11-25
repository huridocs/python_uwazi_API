"""Resolve a wizard's validation choice into a frozen :class:`ValidationSet`.

Automatic sampling reads the instance ONCE, at run creation (before the
generation turn), from the templates the task targets:

1. every entity of the selected templates is listed (bounded by the requested
   size so a huge instance is not paged in full);
2. entities whose MEASURED property already holds a value are kept — they are
   the ones that can be scored automatically;
3. the first ``sample_size`` in the instance's own order are frozen with that
   value.

A custom filter is a free-text search: it resolves to the entities the search
returns (again narrowed to those carrying the value). When the filter or the
template yields fewer entities than asked, the set is whatever was found —
never padded, never invented. An empty sample is NOT a validation set: the
caller reports that and the run is created without one.

This is a run-creation read, deliberately outside the holdout wrappers (the set
does not exist yet). It is the LAST read that touches those entities before
they are frozen.
"""

from __future__ import annotations

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
from uwazi_agent.ports.entity_api_port import EntityApiPort

# How many extra entities to inspect to still find ``sample_size`` carrying the
# measured value: the first N of a template may all be empty.
_SCAN_MULTIPLIER: int = 20


def _property_value(entity_any: Any, property_name: str, language: str) -> Any:
    """The measured property's value on one entity, in one language (pure)."""
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


async def resolve_validation_set(
    entity_api: EntityApiPort,
    run_id: str,
    config: ValidationConfig,
    now: datetime | None = None,
) -> ValidationSet | None:
    """Freeze a sample for one run, or None when nothing judgeable was found.

    ``config.mode`` decides the source: ``automatic`` scans the selected
    templates; ``custom`` uses ``filter_text`` as the search term. Both keep
    only entities carrying a value for the measured property. ``none`` returns
    None without a read.
    """
    if config.mode == VALIDATION_NONE or not config.property_name.strip():
        return None
    property_name = validation_property_name(config.property_name)
    wanted = max(1, int(config.sample_size))
    limit = wanted * _SCAN_MULTIPLIER

    candidates: list[Any] = []
    if config.mode == VALIDATION_CUSTOM and config.filter_text.strip():
        result = await entity_api.search_entities_by_text(
            search_term=config.filter_text.strip(),
            template_name=config.template_names[0] if config.template_names else None,
            language=config.language,
            limit=limit,
        )
        candidates = list(result._all_entities) or list(result.examples)
    elif config.mode in (VALIDATION_AUTOMATIC, VALIDATION_CUSTOM):
        templates = config.template_names or [None]
        for template_name in templates:
            if template_name is None:
                continue
            found = await entity_api.get_entities_by_template(template_name, config.language, limit)
            candidates.extend(list(found._all_entities) or list(found.examples))

    shared_ids: list[str] = []
    labels: dict[str, Any] = {}
    for entity in candidates:
        if len(shared_ids) >= wanted:
            break
        value = _property_value(entity, property_name, config.language)
        if not _has_value(value):
            continue
        shared_id = str(getattr(entity, "shared_id", "") or "")
        if not shared_id or shared_id in shared_ids:
            continue
        shared_ids.append(shared_id)
        labels[label_key(shared_id, config.language)] = value

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
        filter_text=config.filter_text,
        created_at=now or datetime.now(timezone.utc),
    )


__all__ = ["resolve_validation_set"]
