"""Sandbox namespace module split out of ``script_exec_namespace`` (behavior preserved)."""

from __future__ import annotations

from collections.abc import Iterable
from types import SimpleNamespace
from typing import Any

from uwazi_admin_agent.domain.suggestion import TITLE_PROPERTY

from .constants import NamedNamespace


def scope_read_result(result: Any, scope: set[str]) -> Any:
    """Post-filter a bound read helper's result to ``scope`` (pure).

    The subset-prediction mechanism: the SAME script runs unchanged, but a
    ``query_entities``/``query_entities_full`` call sees only the selected
    entities. Silent drop is the semantic (a script that asks for the whole
    template gets its scope's slice) — deliberately not an exception, because
    the script's own logic is scope-blind by design.

    Handles every shape the helpers return: the error string passes through
    untouched, ``by_ids``/``query_entities_full`` return ``list[dict]``, and the
    search modes return a ``SimpleNamespace(summary, examples)`` whose summary
    id list + count are re-derived. Anything else (an unwired stub's raise never
    reaches here) is returned as-is.
    """
    if isinstance(result, str):
        return result
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict) and str(item.get("shared_id") or "") in scope]
    if isinstance(result, SimpleNamespace):
        examples = [
            example
            for example in (result.examples or [])
            if isinstance(example, dict) and str(example.get("shared_id") or "") in scope
        ]
        summary = result.summary
        ids = [sid for sid in summary.shared_ids if sid in scope]
        by_template: dict[str, int] = {}
        for example in examples:
            name = str(example.get("template_name") or "")
            if name:
                by_template[name] = by_template.get(name, 0) + 1
        updated = summary.model_copy(update={"shared_ids": ids, "count": len(ids)})
        return NamedNamespace(summary=updated, examples=examples)
    return result


def _scoped_read_wrapper(base: Any, scope: set[str]) -> Any:
    """Wrap a bound read helper so its result keeps only the in-scope ids."""

    def filtered(*args: Any, **kwargs: Any) -> Any:
        return scope_read_result(base(*args, **kwargs), scope)

    return filtered


def exclude_read_result(result: Any, excluded: set[str]) -> Any:
    """Post-filter a bound read helper's result to DROP ``excluded`` ids (pure).

    The deny-list counterpart of :func:`scope_read_result`: a production pass
    with no allow-list still must not see the run's holdout entities, so the
    same shapes are filtered with the opposite predicate.
    """
    if not excluded:
        return result
    if isinstance(result, str):
        return result
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict) and str(item.get("shared_id") or "") not in excluded]
    if isinstance(result, SimpleNamespace):
        examples = [
            example
            for example in (result.examples or [])
            if isinstance(example, dict) and str(example.get("shared_id") or "") not in excluded
        ]
        summary = result.summary
        ids = [sid for sid in summary.shared_ids if sid not in excluded]
        return NamedNamespace(summary=summary.model_copy(update={"shared_ids": ids, "count": len(ids)}), examples=examples)
    return result


def _excluding_read_wrapper(base: Any, excluded: set[str]) -> Any:
    """Wrap a bound read helper so its result DROPS the excluded ids."""

    def filtered(*args: Any, **kwargs: Any) -> Any:
        return exclude_read_result(base(*args, **kwargs), excluded)

    return filtered


def _excluded_entity_reads(namespace: dict[str, Any], excluded: set[str]) -> None:
    """Hide ``excluded`` entities from a namespace that has NO allow-list.

    Exactly the helpers :func:`_scoped_entity_reads` rewrites, with the deny
    predicate instead: discovery drops the held-out ids, and the per-entity
    readers report the same "missing" answer they give for an entity without
    files/segmentation, so a script's own per-entity loop skips them.
    """
    namespace["query_entities"] = _excluding_read_wrapper(namespace["query_entities"], excluded)
    namespace["query_entities_full"] = _excluding_read_wrapper(namespace["query_entities_full"], excluded)

    base_files = namespace["get_entity_files"]

    def get_entity_files(shared_id: str, language: str | None = None) -> Any:
        if shared_id in excluded:
            return []
        return base_files(shared_id, language)

    namespace["get_entity_files"] = get_entity_files

    base_segmentation = namespace["get_segmentation"]

    def get_segmentation(shared_id: str, language: str | None = None) -> Any:
        if shared_id in excluded:
            return None
        return base_segmentation(shared_id, language)

    namespace["get_segmentation"] = get_segmentation


def _scoped_entity_reads(
    namespace: dict[str, Any],
    scope: set[str],
    exclude: set[str] | None = None,
) -> None:
    """Post-filter a dry-run namespace's entity reads to ``scope`` IN PLACE.

    Only the dry run takes a subset scope (predictions are the rehearsal), so
    this rewrites the two discovery helpers plus the two per-entity readers:

    - ``query_entities`` / ``query_entities_full``: results filtered (see
      :func:`scope_read_result`);
    - ``get_entity_files``: ``[]`` for a sharedId outside the scope (the script's
      per-entity loop must skip it exactly as it would for an entity with no
      files);
    - ``get_segmentation``: ``None`` for a sharedId outside the scope (same
      "missing, continue" contract the helper already documents).
    """
    excluded = exclude or set()
    namespace["query_entities"] = _scoped_read_wrapper(namespace["query_entities"], scope - excluded)
    namespace["query_entities_full"] = _scoped_read_wrapper(namespace["query_entities_full"], scope - excluded)

    base_files = namespace["get_entity_files"]

    def get_entity_files(shared_id: str, language: str | None = None) -> Any:
        if shared_id not in scope or shared_id in excluded:
            return []
        return base_files(shared_id, language)

    namespace["get_entity_files"] = get_entity_files

    base_segmentation = namespace["get_segmentation"]

    def get_segmentation(shared_id: str, language: str | None = None) -> Any:
        if shared_id not in scope or shared_id in excluded:
            return None
        return base_segmentation(shared_id, language)

    namespace["get_segmentation"] = get_segmentation


def full_mode_usage_error(mode: str) -> str:
    """The unknown-mode error BOTH ``query_entities_full`` factories return.

    ``by_ids`` is deliberately absent from the mode list: ``query_entities_full``
    exists so scripts never re-fetch a search's entities one id at a time;
    ids-only or known-id reads belong to ``query_entities``. The error says
    so, keeping the two helpers' contracts unambiguous. Pure.
    """
    return (
        f"Error: unknown mode '{mode}'. Use one of: 'by_text', 'by_filter', 'by_template'. "
        "(For known shared_ids use query_entities(mode='by_ids', shared_ids=[...]).)"
    )


# --- measured-property guard -------------------------------------------------

# The key a per-language property's value is stored under inside a raw/entity
# dict, and the metadata key Uwazi uses for the title itself.
_METADATA_KEY = "metadata"


def strip_measured_property(entity: dict[str, Any], property_name: str) -> dict[str, Any]:
    """One entity dict with the MEASURED property's current value removed (pure).

    The value a run is scoring must never reach the script that proposes it: a
    script that can read today's ``states`` links can "propose" them back (or
    merge them into its extraction), which is exactly how a validation sample
    ended up carrying a state the document never mentioned — the suggestion was
    contaminated with the very value the score measures.

    Only the measured property is removed (and ``title`` when the measured
    property IS the title). Every other property stays readable, so a task that
    legitimately derives a value from other entity data keeps working; the
    proposed property simply cannot be used as its own input.

    The caller's dict is not mutated: a copy is returned, so the read helper's
    other consumers (and any cached raw) are unaffected. A dict without the key
    is returned as-is.
    """
    if not isinstance(entity, dict):
        return entity
    if property_name == TITLE_PROPERTY:
        if "title" not in entity:
            return entity
        return {key: value for key, value in entity.items() if key != "title"}
    metadata = entity.get(_METADATA_KEY)
    if not isinstance(metadata, dict) or property_name not in metadata:
        return entity
    stripped = {key: value for key, value in metadata.items() if key != property_name}
    return {**entity, _METADATA_KEY: stripped}


def strip_property_from_result(result: Any, property_name: str) -> Any:
    """Apply :func:`strip_measured_property` to whatever shape a read returns (pure).

    Handles the same shapes as :func:`scope_read_result`: an error string passes
    through, a ``by_ids``/``query_entities_full`` list has every entity stripped,
    and a search ``SimpleNamespace`` has its ``examples`` stripped (its summary
    carries ids and counts, never values).
    """
    if isinstance(result, str):
        return result
    if isinstance(result, list):
        return [strip_measured_property(item, property_name) for item in result]
    if isinstance(result, SimpleNamespace):
        examples = [strip_measured_property(example, property_name) for example in (result.examples or [])]
        return NamedNamespace(summary=result.summary, examples=examples)
    return result


def _measured_property_guarded_wrapper(base: Any, property_name: str) -> Any:
    """Wrap a bound read helper so the measured property's value is never returned."""

    def filtered(*args: Any, **kwargs: Any) -> Any:
        return strip_property_from_result(base(*args, **kwargs), property_name)

    return filtered


def _measured_property_guarded_reads(namespace: dict[str, Any], property_name: str) -> None:
    """Hide ONE property's stored value from an entity-discovery namespace (in place).

    The deny-list is per property, not per entity: the script still reads the
    entities it must process (titles, documents, other properties) and only the
    value it is supposed to COMPUTE is missing. That makes "propose a value from
    the document" a property of the sandbox rather than of the model's goodwill,
    which is what keeps a holdout score honest.
    """
    if not property_name:
        return
    namespace["query_entities"] = _measured_property_guarded_wrapper(namespace["query_entities"], property_name)
    namespace["query_entities_full"] = _measured_property_guarded_wrapper(namespace["query_entities_full"], property_name)


def hidden_measured_property(measured_property: str, input_properties: Iterable[str] | None = None) -> str | None:
    """The property whose stored value a planning pass must not read (pure).

    The rule the operator chose in the wizard decides it: a value is a legitimate
    INPUT only when the wizard declared it as one (a source metadata property,
    or the target property itself when the scope is "only entities where it is
    currently empty"). Otherwise the property being proposed is also the
    property being measured, and letting the script read today's value turns
    "extract it from the document" into "echo what is already stored" — which
    scores perfectly while proving nothing, and contaminates the validation
    sample with the very value the score compares against.

    ``None`` means nothing is hidden (no measured property). The title alias is
    normalized so ``title`` and ``_title`` name the same value.
    """
    name = (measured_property or "").strip()
    if not name:
        return None
    declared = {str(prop).strip() for prop in (input_properties or ()) if str(prop).strip()}
    if name in declared or (name == TITLE_PROPERTY and "title" in declared):
        return None
    return name
