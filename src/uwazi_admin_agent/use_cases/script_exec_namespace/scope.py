"""Sandbox namespace module split out of ``script_exec_namespace`` (behavior preserved)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from uwazi_agent.domain.agent_entity import AgentEntity


class ScopeViolationError(RuntimeError):
    """A script helper was asked to touch a shared_id outside the dummy scope."""


def filter_ids_to_scope(shared_ids: list[str], scope: set[str]) -> list[str]:
    """Return the requested ids that are within ``scope``, preserving order."""
    return [sid for sid in shared_ids if sid in scope]


def assert_ids_in_scope(shared_ids: list[str], scope: set[str], helper: str) -> None:
    """Raise :class:`ScopeViolationError` if any id is outside ``scope``.

    Pure: the dummy-scoped write helpers call this before forwarding to the
    reused CRUD function, so a script cannot mutate a real entity during
    validation even if it hardcodes a real shared_id.
    """
    offenders = [sid for sid in shared_ids if sid not in scope]
    if offenders:
        raise ScopeViolationError(
            f"{helper}: refused shared_id(s) outside the dummy scope: {offenders}. "
            "The validation script may only touch the throwaway dummies."
        )


def filter_in_scope_by_template(in_scope: list[AgentEntity], template_name: str | None) -> list[AgentEntity]:
    """Return the in-scope dummies belonging to ``template_name`` (dummy mode).

    In real mode ``by_template`` returns only the entities of that template; the
    dummy wrapper must mirror that so a script that loops ``by_template`` over
    several discovered templates processes each template's dummies once.
    Otherwise every ``by_template`` call returns ALL in-scope dummies and the
    gate false-fails a correct multi-template script: deleted source dummies
    reappear via the static ``dummy_entities`` list on the next iteration and
    get re-processed (double merge / delete-already-deleted).

    Pure: the testable seam extracted from :func:`_sync_query_entities_factory`.
    A ``None`` template_name returns the list unchanged (defensive - the
    ``by_template`` mode requires a template_name, but the helper stays total).
    """
    if not template_name:
        return in_scope
    return [e for e in in_scope if e.template_name == template_name]


def scope_read_result(result: Any, scope: set[str]) -> Any:
    """Post-filter a bound read helper's return value to ``scope`` (pure).

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
        ids = filter_ids_to_scope(list(summary.shared_ids), scope)
        return SimpleNamespace(summary=summary.model_copy(update={"shared_ids": ids, "count": len(ids)}), examples=examples)
    return result


def _scoped_read_wrapper(base: Any, scope: set[str]) -> Any:
    """Wrap a bound read helper so its result is post-filtered to ``scope``."""

    def scoped(*args: Any, **kwargs: Any) -> Any:
        return scope_read_result(base(*args, **kwargs), scope)

    return scoped


def _scoped_entity_reads(
    namespace: dict[str, Any],
    scope: set[str],
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
    namespace["query_entities"] = _scoped_read_wrapper(namespace["query_entities"], scope)
    namespace["query_entities_full"] = _scoped_read_wrapper(namespace["query_entities_full"], scope)

    base_files = namespace["get_entity_files"]

    def get_entity_files(shared_id: str, language: str | None = None) -> Any:
        if shared_id not in scope:
            return []
        return base_files(shared_id, language)

    namespace["get_entity_files"] = get_entity_files

    base_segmentation = namespace["get_segmentation"]

    def get_segmentation(shared_id: str, language: str | None = None) -> Any:
        if shared_id not in scope:
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
