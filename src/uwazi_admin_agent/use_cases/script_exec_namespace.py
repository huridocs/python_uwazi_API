"""Build the exec namespace the generated script runs in (§2.1, §2.7, Phase 3).

The script is **target-agnostic**: it discovers its target set at runtime via the
bound sync ``query_entities`` and mutates via the bound sync write helpers. The
*safety boundary is the exec namespace* (§2.1): the script can only do what the
bound names let it. Phase 3 wires a **dummy-scoped** namespace so the identical
script runs against throwaway dummies; Phase 4 will wire a real-scoped +
backup-intercepted namespace reusing the same shape.

This module:
- reuses ``uwazi_agent``'s sync CRUD-helper factory
  (``_build_sync_crud_functions`` — the ``python_code_executor`` machinery named
  in §3) for the write helpers, then **scopes** them to the dummy shared-id set
  (refuse out-of-scope ids; ``create_entities`` records new ids into the live
  scope set so cleanup is guaranteed);
- adds a **sync ``query_entities`` wrapper** (NEW — ``python_code_executor``
  exposes no sync read helper), which in dummy mode returns only the dummies in
  the shapes the system prompt declares (``AgentEntitySearchResult`` for search
  modes, ``list[AgentEntity]`` for ``by_ids``), post-filtered to scope;
- binds the declared stdlib subset (``json, re, collections, itertools,
  datetime, math``) and a curated ``SAFE_BUILTINS`` (no ``open``/``__import__``
  /``eval``/``exec``/``compile``/``getattr``/...);
- provides **no** ``entities`` list (discovery is runtime).

The pure scoping helpers (:func:`filter_ids_to_scope`,
:func:`assert_ids_in_scope`) and :data:`SAFE_BUILTINS` are unit-tested; the full
namespace build wires an event loop + ports and is validated via the simulation
run.
"""

from __future__ import annotations

import asyncio
import builtins as _builtins
import collections
import contextlib
import io
import itertools
import json
import math
import re
import traceback
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from uwazi_agent.domain.agent_entity import AgentEntity
from uwazi_agent.domain.agent_entity_summary import AgentEntitySummary
from uwazi_agent.ports.entity_api_port import EntityApiPort
from uwazi_agent.ports.relationship_api_port import RelationshipApiPort
from uwazi_agent.use_cases.tools.python_code_executor import _build_sync_crud_functions

# stdlib subset the system prompt promises the script (see system_prompt.py).
_STDLIB: dict[str, Any] = {
    "json": json,
    "re": re,
    "collections": collections,
    "itertools": itertools,
    "datetime": datetime,
    "math": math,
}

# Curated builtins: enough to write the migration scripts, with the
# namespace-escape vectors removed. The PRIMARY safety in dummy mode is the
# shared-id scoping on the write helpers; this is defense-in-depth so a script
# cannot reach ``open``/``os``/``subprocess`` via ``__import__`` or
# ``getattr``-based introspection. Scripts access entity fields through dict
# ``.get()`` / ``[]`` (entities are dicts), never ``getattr``.
_SAFE_BUILTIN_NAMES: tuple[str, ...] = (
    "None",
    "True",
    "False",
    "Ellipsis",
    "NotImplemented",
    "bool",
    "bytes",
    "bytearray",
    "complex",
    "dict",
    "float",
    "frozenset",
    "int",
    "list",
    "set",
    "str",
    "tuple",
    "range",
    "enumerate",
    "zip",
    "map",
    "filter",
    "sorted",
    "reversed",
    "iter",
    "next",
    "slice",
    "divmod",
    "pow",
    "abs",
    "round",
    "sum",
    "min",
    "max",
    "any",
    "all",
    "bin",
    "hex",
    "oct",
    "chr",
    "ord",
    "ascii",
    "format",
    "repr",
    "len",
    "hash",
    "isinstance",
    "issubclass",
    "callable",
    "print",
    "Exception",
    "BaseException",
    "StopIteration",
    "StopAsyncIteration",
    "ArithmeticError",
    "ZeroDivisionError",
    "OverflowError",
    "FloatingPointError",
    "AssertionError",
    "AttributeError",
    "BufferError",
    "EOFError",
    "ImportError",
    "ModuleNotFoundError",
    "IndexError",
    "KeyError",
    "KeyboardInterrupt",
    "LookupError",
    "MemoryError",
    "NameError",
    "NotImplementedError",
    "OSError",
    "RecursionError",
    "ReferenceError",
    "RuntimeError",
    "SystemError",
    "TypeError",
    "UnboundLocalError",
    "UnicodeError",
    "UnicodeDecodeError",
    "UnicodeEncodeError",
    "ValueError",
    "Warning",
    "DeprecationWarning",
    "UserWarning",
    "GeneratorExit",
    "SystemExit",
)
SAFE_BUILTINS: dict[str, Any] = {name: getattr(_builtins, name) for name in _SAFE_BUILTIN_NAMES}

# Names explicitly NOT in SAFE_BUILTINS (documented; asserted in tests):
#   open, __import__, eval, exec, compile, globals, locals, vars, dir,
#   getattr, setattr, delattr, hasattr, input, breakpoint, help, exit, quit,
#   classmethod, staticmethod, super, object, type, property, memoryview.


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


def _sync_query_entities_factory(
    dummy_entities: list[AgentEntity],
    scope: set[str],
) -> Any:
    """Build the sync, dummy-scoped ``query_entities`` bound into the namespace.

    In dummy mode discovery is a no-op against Uwazi: the harness already created
    the dummies, so the wrapper returns them (filtered to ``scope``) in the
    shapes the system prompt declares — a result object
    (``.summary`` + ``.examples``, a SimpleNamespace with dict examples) for the search
    modes and a ``list[dict]`` (``model_dump``) for ``by_ids``. Returning dicts matches
    the dict-access idioms the CRUD helpers use (create/update take dict literals).
    This guarantees the script can only ever see dummies during validation,
    regardless of what it searches for.
    """

    def query_entities(
        mode: str,
        language: str = "en",
        limit: int = 10000,
        search_term: str | None = None,
        template_name: str | None = None,
        filters: list | None = None,
        published: bool | None = None,
        shared_ids: list[str] | None = None,
    ) -> Any:  # dict list (by_ids) or SimpleNamespace (search) or str (error)
        _ = language, limit, search_term, template_name, filters, published  # search criteria ignored under scoping
        in_scope = [e for e in dummy_entities if e.shared_id in scope]
        if mode == "by_ids":
            if not shared_ids:
                return "Error: 'by_ids' mode requires `shared_ids` (a non-empty list)."
            wanted = filter_ids_to_scope(shared_ids, scope)
            by_id = {e.shared_id: e for e in in_scope}
            # Return dicts (model_dump): the system prompt declares by_ids returns
            # "a list of entity dicts", matching the dict-access idioms the CRUD
            # helpers use (create/update take dict literals).
            return [by_id[sid].model_dump() for sid in wanted if sid in by_id]
        if mode in ("by_text", "by_filter", "by_template"):
            summary = AgentEntitySummary(
                count=len(in_scope),
                by_template={},
                sample_titles=[e.title for e in in_scope[:5]],
                shared_ids=[e.shared_id for e in in_scope],
            )
            # A result object with `.summary` and `.examples` (dicts). The script
            # reads `summary.shared_ids`, then fetches full dicts via `by_ids`.
            return SimpleNamespace(
                summary=summary,
                examples=[e.model_dump() for e in in_scope[:5]],
            )
        return f"Error: unknown mode '{mode}'. Use one of: 'by_text', 'by_filter', 'by_template', 'by_ids'."

    return query_entities


def _scoped_write_helpers(
    crud: tuple,
    scope: set[str],
) -> dict[str, Any]:
    """Wrap the reused sync CRUD helpers with dummy-scope enforcement.

    ``create_entities`` is allowed (it makes new throwaway entities) and records
    each new shared_id into the live ``scope`` set so cleanup covers them. The
    mutating helpers (``update``/``delete``/publish/``create_relationships``)
    refuse any shared_id outside ``scope``. ``create_relationships`` requires
    both endpoints in scope.
    """
    create_entities = crud[0]
    update_entities = crud[1]
    delete_entities = crud[2]
    publish_entities = crud[3]
    unpublish_entities = crud[4]
    set_publish_status = crud[5]
    create_relationships = crud[6]

    def create_entities_scoped(entities_dicts: list[dict], language: str | None = None) -> list[dict]:
        results = create_entities(entities_dicts, language)
        for r in results:
            sid = r.get("shared_id") if isinstance(r, dict) else None
            if sid:
                scope.add(sid)
        return results

    def update_entities_scoped(entities_dicts: list[dict], language: str | None = None) -> list[dict]:
        assert_ids_in_scope([e.get("shared_id") for e in entities_dicts if e.get("shared_id")], scope, "update_entities")
        return update_entities(entities_dicts, language)

    def delete_entities_scoped(shared_ids: list[str]) -> list[dict]:
        assert_ids_in_scope(shared_ids, scope, "delete_entities")
        return delete_entities(shared_ids)

    def set_publish_status_scoped(shared_ids: list[str], published: bool) -> list[dict]:
        assert_ids_in_scope(shared_ids, scope, "set_publish_status")
        return set_publish_status(shared_ids, published)

    def publish_entities_scoped(shared_ids: list[str]) -> dict:
        assert_ids_in_scope(shared_ids, scope, "publish_entities")
        return publish_entities(shared_ids)

    def unpublish_entities_scoped(shared_ids: list[str]) -> dict:
        assert_ids_in_scope(shared_ids, scope, "unpublish_entities")
        return unpublish_entities(shared_ids)

    def create_relationships_scoped(relationships_dicts: list[dict], language: str | None = None) -> list[dict]:
        ids: list[str] = []
        for r in relationships_dicts:
            for key in ("from_entity_shared_id", "to_entity_shared_id"):
                if r.get(key):
                    ids.append(r[key])
        assert_ids_in_scope(ids, scope, "create_relationships")
        return create_relationships(relationships_dicts, language)

    return {
        "create_entities": create_entities_scoped,
        "update_entities": update_entities_scoped,
        "delete_entities": delete_entities_scoped,
        "publish_entities": publish_entities_scoped,
        "unpublish_entities": unpublish_entities_scoped,
        "set_publish_status": set_publish_status_scoped,
        "create_relationships": create_relationships_scoped,
    }


def build_exec_namespace(
    entity_api: Any,
    relationship_api: Any,
    loop: asyncio.AbstractEventLoop,
    scope: set[str],
    dummy_entities: list[AgentEntity],
    tool_cache: Any,
    default_language: str,
) -> dict[str, Any]:
    """Construct the dummy-scoped exec namespace for the candidate script.

    ``scope`` is the **live** set of dummy shared_ids: created dummies are added
    to it by the scoped ``create_entities`` so cleanup can delete them. The
    reused sync CRUD helpers (from ``_build_sync_crud_functions``) are wrapped to
    refuse out-of-scope ids. The bound stdlib subset and ``SAFE_BUILTINS`` make
    the namespace the §2.1 safety boundary. Returns the namespace dict ready for
    ``exec``; the caller reads ``namespace["result"]`` afterwards.
    """
    crud = _build_sync_crud_functions(entity_api, relationship_api, default_language, loop, tool_cache)
    namespace: dict[str, Any] = {
        "query_entities": _sync_query_entities_factory(dummy_entities, scope),
        **_scoped_write_helpers(crud, scope),
        **_STDLIB,
        "__builtins__": SAFE_BUILTINS,
    }
    return namespace


def _real_sync_query_entities_factory(
    entity_api: EntityApiPort,
    loop: asyncio.AbstractEventLoop,
) -> Any:
    """Build the sync, real-scoped ``query_entities`` that actually calls EntityApiPort.

    Mirrors the dummy wrapper's return shapes exactly (``list[dict]`` for
    ``by_ids``, ``SimpleNamespace(summary, examples)`` for search modes) so the
    identical script runs in both validation and execution. Calls the
    ``EntityApiPort`` async methods via ``loop.run_until_complete``.
    """

    def query_entities(
        mode: str,
        language: str = "en",
        limit: int = 10000,
        search_term: str | None = None,
        template_name: str | None = None,
        filters: list | None = None,
        published: bool | None = None,
        shared_ids: list[str] | None = None,
    ) -> Any:
        if mode == "by_ids":
            if not shared_ids:
                return "Error: 'by_ids' mode requires `shared_ids` (a non-empty list)."
            entities = loop.run_until_complete(
                entity_api.get_entities_by_shared_ids(shared_ids=shared_ids, language=language, limit=limit)
            )
            return [e.model_dump() for e in entities]
        if mode == "by_text":
            if not search_term:
                return "Error: 'by_text' mode requires `search_term`."
            result = loop.run_until_complete(
                entity_api.search_entities_by_text(
                    search_term=search_term, template_name=template_name, language=language, limit=limit
                )
            )
            return SimpleNamespace(summary=result.summary, examples=[e.model_dump() for e in result.examples])
        if mode == "by_filter":
            if not template_name:
                return "Error: 'by_filter' mode requires `template_name`."
            result = loop.run_until_complete(
                entity_api.search_entities_by_filter(
                    template_name=template_name, filters=filters or [], language=language, limit=limit, published=published
                )
            )
            return SimpleNamespace(summary=result.summary, examples=[e.model_dump() for e in result.examples])
        if mode == "by_template":
            if not template_name:
                return "Error: 'by_template' mode requires `template_name`."
            result = loop.run_until_complete(
                entity_api.get_entities_by_template(template_name=template_name, language=language, limit=limit)
            )
            return SimpleNamespace(summary=result.summary, examples=[e.model_dump() for e in result.examples])
        return f"Error: unknown mode '{mode}'. Use one of: 'by_text', 'by_filter', 'by_template', 'by_ids'."

    return query_entities


def build_real_exec_namespace(
    entity_api: EntityApiPort,
    relationship_api: RelationshipApiPort | None,
    loop: asyncio.AbstractEventLoop,
    intercept: Any,
    tool_cache: Any,
    default_language: str,
) -> dict[str, Any]:
    """Construct the real-scoped + backup-intercepted exec namespace (Phase 4).

    Same shape as the dummy namespace (target-agnostic, no ``entities`` list,
    same stdlib subset + ``SAFE_BUILTINS``) but:
    - ``query_entities`` actually calls :class:`EntityApiPort` (async, via the
      loop) and returns the same dict/``SimpleNamespace`` shapes.
    - Write helpers come from ``intercept.decorate(crud)`` — backup-intercepted
      (not scope-restricted). The ``intercept`` is a :class:`BackupIntercept`
      but typed ``Any`` here to keep the namespace builder decoupled from the
      intercept module.
    """
    crud = _build_sync_crud_functions(entity_api, relationship_api, default_language, loop, tool_cache)
    namespace: dict[str, Any] = {
        "query_entities": _real_sync_query_entities_factory(entity_api, loop),
        **intercept.decorate(crud),
        **_STDLIB,
        "__builtins__": SAFE_BUILTINS,
    }
    return namespace


@contextlib.contextmanager
def _captured_stdout():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield buf


def run_script_sync(
    code: str,
    namespace: dict[str, Any],
) -> tuple[str | None, str | None]:
    """``exec`` ``code`` in ``namespace``; return ``(result, error)``.

    On success ``result`` is the script's ``result`` variable (stringified if
    set, else ``None``) and ``error`` is ``None``. On exception ``result`` is
    ``None`` and ``error`` is a compact ``Type: msg`` + traceback tail. Stdout
    is captured (the script's ``print``s do not leak). Pure w.r.t. the namespace
    contents; the side effects are whatever the bound helpers do.
    """
    try:
        with _captured_stdout():
            exec(code, namespace)  # noqa: S102 — the script is the unit of work; namespace is the safety boundary
    except Exception as exc:  # noqa: BLE001 — surface any script error to the gate
        tb = traceback.format_exc()
        return None, f"{type(exc).__name__}: {exc}\n\n{tb}"
    result = namespace.get("result")
    if result is None:
        return None, None
    return str(result), None
