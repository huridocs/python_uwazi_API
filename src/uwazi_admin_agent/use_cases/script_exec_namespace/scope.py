"""Sandbox namespace module split out of ``script_exec_namespace`` (behavior preserved)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

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
