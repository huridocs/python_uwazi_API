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
- adds a **sync ``query_entities_full`` bulk-read wrapper**: the same search
  modes returning EVERY matched entity as a full ``model_dump`` dict (the shape
  ``by_ids`` returns). The search modes already fetch all matches through one
  paged ``/api/search`` call; without this helper a script re-fetches them via
  ``by_ids`` — one HTTP request PER shared_id, minutes at the 10 000-entity
  scale — which is exactly the slow path it exists to replace;
- binds the declared stdlib subset (``json, re, collections, itertools,
  datetime (module), math, random``) and a curated ``SAFE_BUILTINS`` (no ``open``/``__import__``
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
import datetime as _datetime
import io
import itertools
import json
import math
import random
import re
import time
import traceback
from types import SimpleNamespace
from typing import Any

from loguru import logger

from uwazi_admin_agent.domain.file_deletion import group_deletions_by_entity
from uwazi_admin_agent.domain.file_restore import extract_file_refs
from uwazi_admin_agent.domain.html_extract import html_meta, html_tables, html_text, html_title, is_html_ref
from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort
from uwazi_admin_agent.ports.file_repository_port import FileRepositoryPort
from uwazi_admin_agent.use_cases.file_transfer import move_files_for_target
from uwazi_admin_agent.use_cases.parallel_executor import ParallelExecutor
from uwazi_admin_agent.use_cases.parallel_script_helpers import (
    assert_unique_deletion_requests,
    assert_unique_move_targets,
    assert_unique_shared_ids,
    build_parallel_file_cleanup_dry_run_helper,
    build_parallel_file_cleanup_helper,
    build_parallel_file_delete_dry_run_helper,
    build_parallel_file_delete_helper,
    build_parallel_move_files_helper,
    build_parallel_read_helpers,
    build_parallel_write_helpers,
)
from uwazi_admin_agent.use_cases.throttle_controller import ThrottleController
from uwazi_agent.domain.agent_entity import AgentEntity
from uwazi_agent.domain.agent_entity_search_result import AgentEntitySearchResult
from uwazi_agent.domain.agent_entity_summary import AgentEntitySummary
from uwazi_agent.ports.entity_api_port import EntityApiPort
from uwazi_agent.ports.relationship_api_port import RelationshipApiPort
from uwazi_agent.use_cases.tools.python_code_executor import _build_sync_crud_functions

# stdlib subset the system prompt promises the script (see system_prompt.py).
# `datetime` is bound as the MODULE (``import datetime``), NOT the class, so the
# script uses the standard ``datetime.datetime.now()`` / ``datetime.timedelta()``
# idiom (matches the LLM's natural instinct and gives ``timedelta``/``date``/
# ``time`` for free). ``random`` is pure-compute (no I/O) and is the natural tool
# for "fill with random information" create scripts.
# Curated pure HTML-extraction API (domain/html_extract.py) bound as a
# namespace so extraction scripts can parse supporting-file HTML without any
# import (bs4/lxml are unavailable; this is stdlib html.parser under the hood).
_HTMLEXTRACT: Any = SimpleNamespace(
    text=html_text,
    title=html_title,
    tables=html_tables,
    meta=html_meta,
    is_html=is_html_ref,
)

# stdlib subset the system prompt promises the script (see system_prompt.py).

_STDLIB: dict[str, Any] = {
    "json": json,
    "re": re,
    "collections": collections,
    "itertools": itertools,
    "datetime": _datetime,
    "math": math,
    "random": random,
    "htmlextract": _HTMLEXTRACT,
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


# The three search modes both bound read helpers share (the engine lives in
# ``_run_search_mode``; ``by_ids`` is a ``query_entities``-only mode).
_SEARCH_MODES: tuple[str, ...] = ("by_text", "by_filter", "by_template")


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
            # by_template must filter by template_name (mirror real mode) so a
            # script looping by_template over several discovered templates
            # processes each template's dummies once - otherwise every call
            # returns ALL in-scope dummies and the gate false-fails a correct
            # multi-template script. by_text/by_filter stay unfiltered (fuzzy
            # text / property filters can't be simulated on dummies).
            selected = filter_in_scope_by_template(in_scope, template_name) if mode == "by_template" else in_scope
            summary = AgentEntitySummary(
                count=len(selected),
                by_template={},
                sample_titles=[e.title for e in selected[:5]],
                shared_ids=[e.shared_id for e in selected],
            )
            # A result object with `.summary` and `.examples` (dicts). The script
            # reads `summary.shared_ids`, then fetches full dicts via `by_ids`.
            return SimpleNamespace(
                summary=summary,
                examples=[e.model_dump() for e in selected[:5]],
            )
        return f"Error: unknown mode '{mode}'. Use one of: 'by_text', 'by_filter', 'by_template', 'by_ids'."

    return query_entities


def _sync_full_entities_factory(
    dummy_entities: list[AgentEntity],
    scope: set[str],
) -> Any:
    """Build the sync, dummy-scoped ``query_entities_full`` bound into the namespace.

    Mirrors the real helper's contract for validation: the same three search
    modes, returning the in-scope dummies as FULL entity dicts (``model_dump``)
    — the shape ``by_ids`` returns — so the identical script runs in dummy and
    real mode. Search criteria are ignored under scoping and ``by_template``
    filters by ``template_name``, exactly like the dummy ``query_entities``.
    ``by_ids`` is refused (the real helper does not offer it; ids-only and
    known-id reads belong to ``query_entities``). A ``shared_ids`` argument is
    accepted but IGNORED so the LLM's by_ids habit gets the guidance error
    string instead of a TypeError crash.
    """

    def query_entities_full(
        mode: str,
        language: str = "en",
        limit: int = 10000,
        search_term: str | None = None,
        template_name: str | None = None,
        filters: list | None = None,
        published: bool | None = None,
        shared_ids: list[str] | None = None,
    ) -> Any:  # list[dict] (full entities) or str (error)
        _ = language, limit, search_term, filters, published, shared_ids  # search criteria ignored under scoping
        if mode not in _SEARCH_MODES:
            return full_mode_usage_error(mode)
        in_scope = [e for e in dummy_entities if e.shared_id in scope]
        selected = filter_in_scope_by_template(in_scope, template_name) if mode == "by_template" else in_scope
        return [e.model_dump() for e in selected]

    return query_entities_full


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

    def delete_entities_scoped(shared_ids: list[str], language: str | None = None) -> list[dict]:
        del language  # ignored: delete is by sharedId across ALL language rows
        assert_ids_in_scope(shared_ids, scope, "delete_entities")
        return delete_entities(shared_ids)

    def set_publish_status_scoped(shared_ids: list[str], published: bool, language: str | None = None) -> list[dict]:
        del language  # ignored: publish/unpublish act on all language rows by sharedId
        assert_ids_in_scope(shared_ids, scope, "set_publish_status")
        return set_publish_status(shared_ids, published)

    def publish_entities_scoped(shared_ids: list[str], language: str | None = None) -> dict:
        del language
        assert_ids_in_scope(shared_ids, scope, "publish_entities")
        return publish_entities(shared_ids)

    def unpublish_entities_scoped(shared_ids: list[str], language: str | None = None) -> dict:
        del language
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


def _move_files_noop_scoped(scope: set[str]) -> Any:
    """Build the dummy-scoped no-op ``move_files_to_entity`` bound into the namespace.

    Dummies are created via ``create_entities`` (no uploaded files), so file-move is
    a no-op in validation: the helper scope-asserts the endpoints (defense-in-depth,
    matching the other scoped write helpers) and returns a ``moved=0`` summary.
    The real file-move logic is exercised only live (file ops cannot be
    gate-validated on dummies - same inherent limitation as delete-revert file
    restore, which is also "live confirmation pending").
    """

    def move_files_to_entity(from_shared_ids: list[str], to_shared_id: str, language: str | None = None) -> dict:
        assert_ids_in_scope([*from_shared_ids, to_shared_id], scope, "move_files_to_entity")
        return {"moved": 0, "failed": 0, "skipped": 0, "note": "no-op in validation - dummies carry no uploaded files"}

    return move_files_to_entity


def _move_files_noop_parallel_scoped(scope: set[str]) -> Any:
    """Build the dummy-scoped no-op ``move_files_to_entity_parallel``.

    Mirrors ``_move_files_noop_scoped`` for the moves-list contract: dummies
    carry no uploaded files, so the parallel move is a no-op in validation.
    It keeps the real helper's two up-front guards so the identical script
    fails identically in every mode: :func:`assert_unique_move_targets`
    (the same-row lost-file race guard) and the scope assertion over every
    move's ``from`` + ``to`` ids (defense-in-depth). Returns one ``moved=0``
    summary per move, in input order — the real helper's result shape.
    """

    def move_files_to_entity_parallel(moves: list[dict], language: str | None = None) -> list[dict]:
        del language  # ignored: dummies carry no files to localize
        assert_unique_move_targets(moves)
        ids: list[str] = [sid for move in moves for sid in [*move["from_shared_ids"], move["to_shared_id"]]]
        assert_ids_in_scope(ids, scope, "move_files_to_entity_parallel")
        return [{"to_shared_id": move["to_shared_id"], "moved": 0, "failed": 0, "skipped": 0} for move in moves]

    return move_files_to_entity_parallel


def _dedupe_files_noop_parallel_scoped(scope: set[str]) -> Any:
    """Build the dummy-scoped no-op ``dedupe_entity_files_parallel``.

    Dummies are created via ``create_entities`` (no uploaded files), so
    duplicate-file cleanup is a no-op in validation: the helper keeps the
    real helper's two up-front guards so the identical script fails
    identically in every mode — :func:`assert_unique_shared_ids` (the
    double-delete race guard) and the scope assertion over every shared_id
    (defense-in-depth) — and returns one zero-count summary per entity, in
    input order: the real helper's result shape. The real cleanup logic is
    exercised only live (file ops cannot be gate-validated on dummies — the
    same documented limitation as the file-move helpers).
    """

    def dedupe_entity_files_parallel(shared_ids: list[str], language: str | None = None) -> list[dict]:
        del language  # ignored: dummies carry no files to localize
        assert_unique_shared_ids(shared_ids, "dedupe_entity_files_parallel")
        assert_ids_in_scope(list(shared_ids), scope, "dedupe_entity_files_parallel")
        return [{"shared_id": sid, "duplicates": 0, "deleted": 0, "failed": 0, "kept_cited": 0} for sid in shared_ids]

    return dedupe_entity_files_parallel


def _delete_files_noop_parallel_scoped(scope: set[str]) -> Any:
    """Build the dummy-scoped no-op ``delete_entity_files_parallel``.

    Dummies carry no uploaded files, so explicit deletion is a no-op in
    validation. The helper keeps the real helper's up-front guards so the
    identical script fails identically in every mode —
    :func:`assert_unique_deletion_requests` (the duplicated-request race
    guard), :func:`group_deletions_by_entity` (which also validates every
    request names a shared_id), and the scope assertion over every entity
    (defense-in-depth) — and returns one zero-count summary per DISTINCT
    entity, in first-appearance order: the real helper's result shape.
    """

    def delete_entity_files_parallel(deletions: list[dict], language: str | None = None) -> list[dict]:
        del language  # ignored: dummies carry no files to localize
        assert_unique_deletion_requests(deletions)
        grouped = group_deletions_by_entity(deletions)
        assert_ids_in_scope([sid for sid, _ in grouped], scope, "delete_entity_files_parallel")
        return [
            {"shared_id": sid, "requested": len(requests), "deleted": 0, "failed": 0, "refused": 0, "refusals": []}
            for sid, requests in grouped
        ]

    return delete_entity_files_parallel


def _build_move_files_real_helper(
    entity_repository: EntityRepositoryPort | None,
    file_repository: FileRepositoryPort | None,
    loop: asyncio.AbstractEventLoop,
    default_language: str,
) -> Any:
    """Build the real ``move_files_to_entity`` bound into the real exec namespace.

    The body is the shared :func:`move_files_for_target` flow (the same one
    ``move_files_to_entity_parallel`` runs per target): fetch the target's
    raw and build the dedupe index, then per source — fetch its full raw
    (incl. ``documents``/``attachments``), extract the uploaded-file refs
    (documents + uploaded attachments; URL attachments have no bytes and are
    skipped - moving them is a flagged gap), fetch each file's bytes, SKIP it
    when byte-identical content is already on the target, else re-upload via
    ``upload_document``/``upload_attachment`` (documents first, then
    attachments - ``extract_file_refs`` already returns them in that order).

    Skipping duplicates matters: Uwazi never dedupes uploads (every upload
    mints a fresh file row joined to the entity by ``sharedId``), so without
    it, merging N duplicate entities that share the same files multiplies
    those files N times on the target. A skip is decided by a sha256 match
    only - a same-named but different file still uploads (no-loss bias).

    Best-effort, like delete-revert file restore: a failed fetch/upload is
    counted as ``failed`` but does NOT raise - the merge still deletes the sources
    and the target keeps whatever files were moved. Revert does NOT remove
    moved files: they are file-collection rows joined by ``sharedId``, not
    fields on the entity row a raw restore can rewrite - after revert the
    target still carries the moved copies (orphaned in storage semantics is
    moot; they stay fully visible). The sources ARE re-created with their
    files (the delete-revert path captured their bytes before the delete).

    If either port is None (e.g. tests with no file repository wired), returns a
    stub that raises a clear ``RuntimeError`` when the script actually calls it -
    so an unwired script fails loudly instead of silently dropping files.
    """
    if entity_repository is None or file_repository is None:

        def move_files_to_entity_unwired(from_shared_ids: list[str], to_shared_id: str, language: str | None = None) -> dict:
            raise RuntimeError(
                "move_files_to_entity requires a wired entity_repository and "
                "file_repository (got None). Wire FileRepositoryPort into the "
                "runtime/execute use case to enable file-move for merges."
            )

        return move_files_to_entity_unwired

    def move_files_to_entity(from_shared_ids: list[str], to_shared_id: str, language: str | None = None) -> dict:
        lang = language or default_language
        return move_files_for_target(
            entity_repository, file_repository, from_shared_ids, to_shared_id, lang, loop.run_until_complete
        )

    return move_files_to_entity


def _get_entity_files_noop_scoped(scope: set[str]) -> Any:
    """Build the dummy-scoped no-op ``get_entity_files`` bound into the namespace.

    Dummies are created via ``create_entities`` (no uploaded files), so file
    fetch is a no-op in validation: the helper scope-asserts the shared_id
    (defense-in-depth, matching the other scoped helpers) and returns ``[]``.
    The real fetch logic is exercised only live — same documented limitation as
    ``move_files_to_entity`` (dummies carry no uploaded files).
    """

    def get_entity_files(shared_id: str, language: str | None = None) -> list[dict]:
        del language  # ignored: dummies carry no files to localize
        assert_ids_in_scope([shared_id], scope, "get_entity_files")
        return []

    return get_entity_files


def _get_file_bytes_noop() -> Any:
    """Build the dummy no-op ``get_file_bytes`` (dummies carry no files)."""

    def get_file_bytes(filename: str) -> bytes | None:
        return None

    return get_file_bytes


def _dummy_parallel_helpers(scoped: dict[str, Any], scope: set[str]) -> dict[str, Any]:
    """Bind the ``*_parallel`` names in DUMMY mode: shapes identical, no threads.

    Validation runs against a handful of dummies, so the parallel write names
    simply ALIAS the scoped sequential helpers — same scope enforcement, same
    return shapes — and the read names mirror their single-item no-ops as
    dicts, while the file-move name mirrors ``_move_files_noop_scoped`` as a
    per-move list, the dedupe name mirrors
    ``_dedupe_files_noop_parallel_scoped`` as a per-entity list, and the
    explicit-deletion name mirrors ``_delete_files_noop_parallel_scoped``.
    The identical script therefore runs unchanged in the gate; real
    parallelism (and the throttle) is exercised only live, like file-move.
    """
    return {
        "update_entities_parallel": scoped["update_entities"],
        "create_entities_parallel": scoped["create_entities"],
        "create_relationships_parallel": scoped["create_relationships"],
        "get_entity_files_parallel": _dummy_files_parallel(scope),
        "get_file_bytes_parallel": _dummy_bytes_parallel(),
        "move_files_to_entity_parallel": _move_files_noop_parallel_scoped(scope),
        "dedupe_entity_files_parallel": _dedupe_files_noop_parallel_scoped(scope),
        "delete_entity_files_parallel": _delete_files_noop_parallel_scoped(scope),
    }


def _dummy_files_parallel(scope: set[str]) -> Any:
    """Dummy ``get_entity_files_parallel``: scope-assert, ``[]`` per in-scope id."""

    def get_entity_files_parallel(shared_ids: list[str], language: str | None = None) -> dict[str, list[dict]]:
        del language  # ignored: dummies carry no files to localize
        assert_ids_in_scope(list(shared_ids), scope, "get_entity_files_parallel")
        return {sid: [] for sid in shared_ids}

    return get_entity_files_parallel


def _dummy_bytes_parallel() -> Any:
    """Dummy ``get_file_bytes_parallel``: ``None`` per filename (no stored bytes)."""

    def get_file_bytes_parallel(filenames: list[str]) -> dict[str, bytes | None]:
        return {name: None for name in filenames}

    return get_file_bytes_parallel


def _build_get_entity_files_real_helper(
    entity_repository: EntityRepositoryPort | None,
    loop: asyncio.AbstractEventLoop,
    default_language: str,
) -> Any:
    """Build the real ``get_entity_files`` bound into the real exec namespace.

    Fetches the entity's full raw (incl. ``documents``/``attachments``) and
    returns each uploaded file as a plain dict (``file_id``, ``kind``
    "document"|"attachment", ``filename``, ``originalname``, ``language``,
    ``content_type``). URL attachments are skipped (``extract_file_refs`` has no
    stored bytes for them). Unwired ``entity_repository`` -> a stub that raises
    a clear ``RuntimeError`` when the script calls it (loud, not silent).
    """
    if entity_repository is None:

        def get_entity_files_unwired(shared_id: str, language: str | None = None) -> list[dict]:
            raise RuntimeError(
                "get_entity_files requires a wired entity_repository (got None). "
                "Wire EntityRepositoryPort into the runtime/execute use case to "
                "enable supporting-file extraction."
            )

        return get_entity_files_unwired

    def get_entity_files(shared_id: str, language: str | None = None) -> list[dict]:
        lang = language or default_language
        started = time.monotonic()
        raw = loop.run_until_complete(entity_repository.get_raw_by_shared_id(shared_id, lang))
        refs = [ref.model_dump() for ref in extract_file_refs(raw)]
        # debug, not info: a full pass is tens of thousands of calls — the dry-run/
        # execute boundary logs one aggregate line (incl. cache stats) instead.
        logger.debug("script get_entity_files: {} -> {} file(s) ({:.1f}s)", shared_id, len(refs), time.monotonic() - started)
        return refs

    return get_entity_files


def _build_get_file_bytes_real_helper(file_repository: FileRepositoryPort | None, loop: asyncio.AbstractEventLoop) -> Any:
    """Build the real ``get_file_bytes`` bound into the real exec namespace.

    Returns the file's raw bytes or ``None`` when the file is absent (the
    script counts it as missing and continues — mirrors best-effort
    ``move_files_to_entity``). Unwired ``file_repository`` -> a stub that
    raises a clear ``RuntimeError`` when the script calls it.
    """
    if file_repository is None:

        def get_file_bytes_unwired(filename: str) -> bytes | None:
            raise RuntimeError(
                "get_file_bytes requires a wired file_repository (got None). "
                "Wire FileRepositoryPort into the runtime/execute use case to "
                "enable supporting-file extraction."
            )

        return get_file_bytes_unwired

    def get_file_bytes(filename: str) -> bytes | None:
        started = time.monotonic()
        data = loop.run_until_complete(file_repository.get_file_bytes(filename))
        # debug, not info: same aggregate-not-per-file logging rationale as above.
        logger.debug(
            "script get_file_bytes: {} -> {} bytes ({:.1f}s)",
            filename,
            len(data) if data is not None else 0,
            time.monotonic() - started,
        )
        return data

    return get_file_bytes


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
    scoped = _scoped_write_helpers(crud, scope)
    namespace: dict[str, Any] = {
        "query_entities": _sync_query_entities_factory(dummy_entities, scope),
        "query_entities_full": _sync_full_entities_factory(dummy_entities, scope),
        **scoped,
        **_dummy_parallel_helpers(scoped, scope),
        "move_files_to_entity": _move_files_noop_scoped(scope),
        "get_entity_files": _get_entity_files_noop_scoped(scope),
        "get_file_bytes": _get_file_bytes_noop(),
        **_STDLIB,
        "__builtins__": SAFE_BUILTINS,
    }
    return namespace


# ``by_ids`` is the one script read that hides a long sequential per-entity fetch
# loop inside a single call (the adapter resolves one HTTP request per shared_id).
# Fetching (and logging) in chunks keeps a slow remote instance narrated: one
# INFO line per chunk instead of silence for the whole fetch. Behavior is
# unchanged (same requests, same order, ``limit`` still trims the total set).
# Scripts that need the full entities BEHIND A SEARCH should not pay this loop
# at all: the bound ``query_entities_full`` returns them straight from the
# search (one or two paged /api/search requests - see build_full_entities_view).
_BY_IDS_CHUNK: int = 50


def build_search_result_view(result: AgentEntitySearchResult) -> SimpleNamespace:
    """Build the bound ``query_entities`` search-mode return value (real mode).

    Surfaces the FULL list of shared_ids (from ``result._all_entities``) instead
    of the LLM-context-truncated ``result.summary.shared_ids`` (capped to 3 by
    ``UwaziApiAdapter._summarize`` for the agent tool). A generated script that
    discovers entities to group/merge needs every id, not a 3-sample; the
    truncation is an LLM-context concern the bound script helper must not inherit.
    Reading ``result._all_entities`` mirrors ``uwazi_agent``'s own tools
    (``query_entities.py`` etc.); no ``uwazi_api``/``uwazi_agent`` modification.

    Pure: the testable seam extracted from :func:`_real_sync_query_entities_factory`.
    The rest of the summary (count, by_template, sample_titles, note) is
    preserved; ``examples`` stays as the sample dicts (the script fetches full
    dicts via ``by_ids``).
    """
    all_entities: list[AgentEntity] = result._all_entities
    full_ids = [e.shared_id for e in all_entities if e.shared_id]
    summary = result.summary.model_copy(update={"shared_ids": full_ids})
    return SimpleNamespace(summary=summary, examples=[e.model_dump() for e in result.examples])


def build_full_entities_view(result: AgentEntitySearchResult) -> list[dict]:
    """Build the bound ``query_entities_full`` search-mode return value (real mode).

    Returns EVERY entity the search matched — from ``result._all_entities`` —
    as the full ``model_dump`` dicts the script's dict-access idioms expect, the
    SAME shape ``by_ids`` returns. This is the bulk-read fast path: the search
    modes already fetch and map every matching entity into ``_all_entities``
    (one or two paged ``/api/search`` requests), so a script gets the whole set
    in ONE helper call instead of re-fetching each id through ``by_ids`` — one
    HTTP request PER entity, minutes at bulk scale (the ~30-minutes-for-10k
    path this helper replaces). Like :func:`build_search_result_view` it reads
    ``result._all_entities`` (what ``uwazi_agent``'s own tools read); no
    ``uwazi_api``/``uwazi_agent`` modification. Order follows the search result
    (the same order ``summary.shared_ids`` carries), so "first entity"
    semantics are unchanged.

    Pure: the testable seam extracted from :func:`_real_full_entities_factory`.
    """
    return [e.model_dump() for e in result._all_entities]


def _run_search_mode(
    entity_api: EntityApiPort,
    loop: asyncio.AbstractEventLoop,
    helper_name: str,
    mode: str,
    language: str,
    limit: int,
    search_term: str | None,
    template_name: str | None,
    filters: list | None,
    published: bool | None,
) -> AgentEntitySearchResult | str:
    """Run one search mode (``by_text``/``by_filter``/``by_template``) against the port.

    The shared engine of the two bound real-mode read helpers —
    ``query_entities`` (summary view) and ``query_entities_full`` (full-dicts
    view) — so their search traffic is IDENTICAL BY CONSTRUCTION: same port
    method, same arguments, same start/completion logging. A missing required
    argument returns the bound helpers' error-string convention; a valid mode
    returns the raw :class:`AgentEntitySearchResult` (its ``_all_entities``
    carries every matched entity; the caller picks the view). Non-search modes
    never reach here — both factories reject them first.
    """
    started = time.monotonic()
    if mode == "by_text":
        if not search_term:
            return "Error: 'by_text' mode requires `search_term`."
        logger.info("script {} by_text: term={!r} template={} limit={}", helper_name, search_term, template_name, limit)
        result = loop.run_until_complete(
            entity_api.search_entities_by_text(
                search_term=search_term, template_name=template_name, language=language, limit=limit
            )
        )
    elif mode == "by_filter":
        if not template_name:
            return "Error: 'by_filter' mode requires `template_name`."
        logger.info(
            "script {} by_filter: template={} filters={} limit={}", helper_name, template_name, len(filters or []), limit
        )
        result = loop.run_until_complete(
            entity_api.search_entities_by_filter(
                template_name=template_name, filters=filters or [], language=language, limit=limit, published=published
            )
        )
    elif mode == "by_template":
        if not template_name:
            return "Error: 'by_template' mode requires `template_name`."
        logger.info("script {} by_template: template={} limit={}", helper_name, template_name, limit)
        result = loop.run_until_complete(
            entity_api.get_entities_by_template(template_name=template_name, language=language, limit=limit)
        )
    else:  # both factories pre-validate the mode; unreachable by a script call
        raise ValueError(f"non-search mode {mode!r} reached _run_search_mode")
    logger.info(
        "script {} {}: {} entities ({:.1f}s)",
        helper_name,
        mode,
        len(result._all_entities),
        time.monotonic() - started,
    )
    return result


def _real_sync_query_entities_factory(
    entity_api: EntityApiPort,
    loop: asyncio.AbstractEventLoop,
) -> Any:
    """Build the sync, real-scoped ``query_entities`` that actually calls EntityApiPort.

    Mirrors the dummy wrapper's return shapes exactly (``list[dict]`` for
    ``by_ids``, ``SimpleNamespace(summary, examples)`` for search modes) so the
    identical script runs in both validation and execution. Calls the
    ``EntityApiPort`` async methods via ``loop.run_until_complete``.

    Every mode logs its start (params) and completion (result count + elapsed),
    and ``by_ids`` logs per chunk: it is the one mode whose single call hides a
    long sequential per-entity fetch (one HTTP request per shared_id), and
    those progress lines are the difference between "slow" and "stuck"
    against a remote instance. When a script needs the ENTITIES behind a
    search (not just their ids), prefer the bound ``query_entities_full``:
    it returns them straight from the search instead of re-fetching per id.
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
        started = time.monotonic()
        if mode == "by_ids":
            if not shared_ids:
                return "Error: 'by_ids' mode requires `shared_ids` (a non-empty list)."
            target_ids = list(shared_ids[:limit])
            logger.info("script query_entities by_ids: fetching {} entities", len(target_ids))
            dumped: list[dict] = []
            for offset in range(0, len(target_ids), _BY_IDS_CHUNK):
                chunk = target_ids[offset : offset + _BY_IDS_CHUNK]
                entities = loop.run_until_complete(
                    entity_api.get_entities_by_shared_ids(shared_ids=chunk, language=language, limit=len(chunk))
                )
                dumped.extend(e.model_dump() for e in entities)
                logger.info(
                    "script query_entities by_ids: {}/{} ids -> {} entities so far ({:.1f}s)",
                    offset + len(chunk),
                    len(target_ids),
                    len(dumped),
                    time.monotonic() - started,
                )
            return dumped
        if mode not in _SEARCH_MODES:
            return f"Error: unknown mode '{mode}'. Use one of: 'by_text', 'by_filter', 'by_template', 'by_ids'."
        outcome = _run_search_mode(
            entity_api, loop, "query_entities", mode, language, limit, search_term, template_name, filters, published
        )
        if isinstance(outcome, str):
            return outcome
        return build_search_result_view(outcome)

    return query_entities


def _real_full_entities_factory(
    entity_api: EntityApiPort,
    loop: asyncio.AbstractEventLoop,
) -> Any:
    """Build the sync, real ``query_entities_full`` that actually calls EntityApiPort.

    The bulk-read counterpart of :func:`_real_sync_query_entities_factory`:
    the same three search modes through the same shared engine
    (:func:`_run_search_mode` — identical traffic by construction), but
    returning the FULL entity dicts (:func:`build_full_entities_view`) instead
    of the summary view, so a script reads a whole search's entities in ONE
    call instead of re-fetching them per-id through ``by_ids`` (one HTTP
    request per entity — unusable at bulk scale). A ``shared_ids`` argument is
    accepted but IGNORED so the LLM's by_ids habit gets the guidance error
    string instead of a TypeError crash.
    """

    def query_entities_full(
        mode: str,
        language: str = "en",
        limit: int = 10000,
        search_term: str | None = None,
        template_name: str | None = None,
        filters: list | None = None,
        published: bool | None = None,
        shared_ids: list[str] | None = None,
    ) -> Any:  # list[dict] (full entities) or str (error)
        _ = shared_ids  # ignored: by_ids reads belong to query_entities
        if mode not in _SEARCH_MODES:
            return full_mode_usage_error(mode)
        outcome = _run_search_mode(
            entity_api, loop, "query_entities_full", mode, language, limit, search_term, template_name, filters, published
        )
        if isinstance(outcome, str):
            return outcome
        return build_full_entities_view(outcome)

    return query_entities_full


def build_real_exec_namespace(
    entity_api: EntityApiPort,
    relationship_api: RelationshipApiPort | None,
    loop: asyncio.AbstractEventLoop,
    intercept: Any,
    tool_cache: Any,
    default_language: str,
    entity_repository: EntityRepositoryPort | None = None,
    file_repository: FileRepositoryPort | None = None,
    throttle: ThrottleController | None = None,
) -> dict[str, Any]:
    """Construct the real-scoped + backup-intercepted exec namespace (Phase 4).

    Same shape as the dummy namespace (target-agnostic, no ``entities`` list,
    same stdlib subset + ``SAFE_BUILTINS``) but:
    - ``query_entities`` (summary view) and ``query_entities_full`` (bulk
      full-entity view) actually call :class:`EntityApiPort` (async, via the
      loop) and return the same dict/``SimpleNamespace`` shapes.
    - Write helpers come from ``intercept.decorate(crud)`` — backup-intercepted
      (not scope-restricted). The ``intercept`` is a :class:`BackupIntercept`
      but typed ``Any`` here to keep the namespace builder decoupled from the
      intercept module.
    - ``move_files_to_entity`` (merge support) is the real mover over
      ``entity_repository`` + ``file_repository``; it is NOT intercept-decorated -
      a merge's target is already snapshotted as ``modified`` by ``update_entities``
      and the sources as ``deleted`` (their files captured) by ``delete_entities``,
      so the moved files are covered by the existing snapshots on revert. If
      either port is None a stub is bound that raises when the script calls it.
      Its ``move_files_to_entity_parallel`` sibling (one task per TARGET, the
      same shared executor/throttle) is not intercept-decorated either, and
      refuses a duplicated ``to_shared_id`` up front — the same-row race that
      drops files.
    - ``dedupe_entity_files_parallel`` (post-merge cleanup) and
      ``delete_entity_files_parallel`` (explicit deletion) likewise are NOT
      intercept-decorated — file deletes are not entity writes, so they ride
      their OWN manifest section (``deleted_files``) instead of a snapshot:
      the shared deletion core persists each file's bytes BEFORE the delete
      call and the helpers record the successful deletes after the batch
      joins, which is what makes both REVERTABLE (revert re-uploads the
      captured bytes; a dedupe revert re-creates its duplicates — the correct
      undo). The dedupe nominator only ever deletes byte-identical duplicates
      (never a connection-cited copy); the explicit nominator refuses +
      reports cited, ambiguous, and unbackable targets.
    - The ``*_parallel`` bulk helpers (auto-throttled, up to
      ``THROTTLE_MAX_WORKERS`` workers) share one :class:`ThrottleController`
      for the whole run; ``throttle`` defaults to a fresh controller so bare
      test/inspection builds stay valid, while :class:`ExecuteScriptUseCase`
      passes its own so the allowance persists across every helper call.
    """
    crud = _build_sync_crud_functions(entity_api, relationship_api, default_language, loop, tool_cache)
    controller = throttle if throttle is not None else ThrottleController()
    executor = ParallelExecutor(controller)
    namespace: dict[str, Any] = {
        "query_entities": _real_sync_query_entities_factory(entity_api, loop),
        "query_entities_full": _real_full_entities_factory(entity_api, loop),
        **intercept.decorate(crud),
        **build_parallel_write_helpers(entity_api, relationship_api, intercept, tool_cache, default_language, executor),
        "move_files_to_entity": _build_move_files_real_helper(entity_repository, file_repository, loop, default_language),
        **build_parallel_move_files_helper(entity_repository, file_repository, default_language, executor),
        **build_parallel_file_cleanup_helper(entity_repository, file_repository, intercept, default_language, executor),
        **build_parallel_file_delete_helper(entity_repository, file_repository, intercept, default_language, executor),
        "get_entity_files": _build_get_entity_files_real_helper(entity_repository, loop, default_language),
        "get_file_bytes": _build_get_file_bytes_real_helper(file_repository, loop),
        **build_parallel_read_helpers(entity_repository, file_repository, default_language, executor),
        **_STDLIB,
        "__builtins__": SAFE_BUILTINS,
    }
    return namespace


def _dry_run_write_helpers(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the 7 write helpers for the dry-run namespace: recorders, no I/O.

    Each helper appends one record per operation into the shared ``records``
    list and returns the same success-shaped value the real helpers return, so
    an identical script runs to completion. Nothing is sent to Uwazi: this is
    the dry-run write boundary (real reads, recorded writes).

    The returned dict ALSO carries the records list itself under the
    underscore-prefixed key ``_dry_run_records`` (the namespace convention for
    non-helper keys) so the caller can read what was recorded after
    :func:`run_script_sync`.
    """

    def create_entities(entities_dicts: list[dict], language: str | None = None) -> list[dict]:
        del language  # recorded per-dict, not as a language override
        logger.info("dry-run record: create_entities x{}", len(entities_dicts))
        for i, d in enumerate(entities_dicts):
            records.append(
                {
                    "op": "create",
                    "title": d.get("title", ""),
                    "template_name": d.get("template_name", ""),
                    "metadata": d.get("metadata", {}),
                }
            )
        return [{"shared_id": f"dry-created-{i}", "success": True} for i, d in enumerate(entities_dicts)]

    def update_entities(entities_dicts: list[dict], language: str | None = None) -> list[dict]:
        logger.info("dry-run record: update_entities x{}", len(entities_dicts))
        for d in entities_dicts:
            records.append(
                {
                    "op": "update",
                    "shared_id": d.get("shared_id"),
                    "template_name": d.get("template_name"),
                    "metadata": d.get("metadata"),
                    "title": d.get("title"),
                    "language": language,
                }
            )
        return [{"shared_id": d.get("shared_id"), "success": True} for d in entities_dicts]

    def delete_entities(shared_ids: list[str]) -> list[dict]:
        logger.info("dry-run record: delete_entities x{}", len(shared_ids))
        for sid in shared_ids:
            records.append({"op": "delete", "shared_id": sid})
        return [{"shared_id": sid, "success": True} for sid in shared_ids]

    def publish_entities(shared_ids: list[str]) -> dict:
        logger.info("dry-run record: publish_entities x{}", len(shared_ids))
        for sid in shared_ids:
            records.append({"op": "publish", "shared_id": sid})
        return _dry_run_publish_summary(len(shared_ids))

    def unpublish_entities(shared_ids: list[str]) -> dict:
        logger.info("dry-run record: unpublish_entities x{}", len(shared_ids))
        for sid in shared_ids:
            records.append({"op": "unpublish", "shared_id": sid})
        return _dry_run_publish_summary(len(shared_ids))

    def set_publish_status(shared_ids: list[str], published: bool) -> list[dict]:
        logger.info("dry-run record: set_publish_status x{} published={}", len(shared_ids), published)
        for sid in shared_ids:
            records.append({"op": "set_publish_status", "shared_id": sid, "published": published})
        return [{"shared_id": sid, "success": True} for sid in shared_ids]

    def create_relationships(relationships_dicts: list[dict], language: str | None = None) -> list[dict]:
        del language
        logger.info("dry-run record: create_relationships x{}", len(relationships_dicts))
        for r in relationships_dicts:
            records.append({"op": "create_relationships", **r})
        return [{"success": True} for _ in relationships_dicts]

    return {
        "create_entities": create_entities,
        "update_entities": update_entities,
        "delete_entities": delete_entities,
        "publish_entities": publish_entities,
        "unpublish_entities": unpublish_entities,
        "set_publish_status": set_publish_status,
        "create_relationships": create_relationships,
        "_dry_run_records": records,
    }


def _dry_run_publish_summary(count: int) -> dict[str, Any]:
    """The summary-dict shape the real ``publish_entities``/``unpublish_entities`` return."""
    return {
        "success_count": count,
        "failure_count": 0,
        "rate_limited": [],
        "permission_denied": [],
        "not_found": [],
        "errors": [],
    }


def build_dry_run_namespace(
    entity_api: EntityApiPort | None,
    loop: asyncio.AbstractEventLoop,
    file_repository: FileRepositoryPort | None,
    default_language: str,
    dry_run_records: list[dict[str, Any]],
    entity_repository: EntityRepositoryPort | None = None,
    throttle: ThrottleController | None = None,
) -> dict[str, Any]:
    """Construct the dry-run exec namespace: REAL reads, recorded writes.

    Composed from the same factories as :func:`build_real_exec_namespace` —
    that is the point of the dry run (the extraction logic, the ``ctx``
    contract, and the update-dict build run for real):
    - ``query_entities`` / ``query_entities_full`` / ``get_entity_files`` /
      ``get_file_bytes`` are the
      REAL helpers (live reads against the wired ports; the factory's
      unwired stubs raise a clear ``RuntimeError`` when the port is ``None``);
    - all 7 write helpers plus ``move_files_to_entity`` are pure recorders
      appending into ``dry_run_records`` — no I/O at all, so the dry run can
      never mutate Uwazi. The ``*_parallel`` write names alias the same
      recorders (same shapes, recorded exactly like the sequential call),
      ``move_files_to_entity_parallel`` records one ``move_files`` op per
      move (and keeps the real helper's duplicated-target guard), while the
      ``*_parallel`` READ names are the real auto-throttled helpers — the
      read pass is where dry-run minutes go, so it gets the same
      parallelism + throttle the execute pass will use.
      ``dedupe_entity_files_parallel`` and ``delete_entity_files_parallel``
      are the hybrids their safety role demands: REAL discovery reads (so
      the rehearsal sees the true duplicate groups / refusals) with pure
      recording — one ``delete_file`` op per would-be delete (plus a
      ``refuse_file`` op per explicit-deletion refusal, reason included) —
      the operator's review copy for operations whose undo is a re-upload,
      not an un-record.
    """
    controller = throttle if throttle is not None else ThrottleController()
    executor = ParallelExecutor(controller)
    dry_writes = _dry_run_write_helpers(dry_run_records)
    namespace: dict[str, Any] = {
        "query_entities": (
            _dry_run_query_entities_unwired() if entity_api is None else _real_sync_query_entities_factory(entity_api, loop)
        ),
        "query_entities_full": (
            _dry_run_full_entities_unwired() if entity_api is None else _real_full_entities_factory(entity_api, loop)
        ),
        **dry_writes,
        **_dry_run_parallel_write_aliases(dry_writes),
        "move_files_to_entity": _dry_run_move_files_helper(dry_run_records),
        "move_files_to_entity_parallel": _dry_run_move_files_parallel_helper(dry_run_records),
        **build_parallel_file_cleanup_dry_run_helper(
            entity_repository, file_repository, default_language, executor, dry_run_records
        ),
        **build_parallel_file_delete_dry_run_helper(
            entity_repository, file_repository, default_language, executor, dry_run_records
        ),
        "get_entity_files": _build_get_entity_files_real_helper(entity_repository, loop, default_language),
        "get_file_bytes": _build_get_file_bytes_real_helper(file_repository, loop),
        **build_parallel_read_helpers(entity_repository, file_repository, default_language, executor),
        **_STDLIB,
        "__builtins__": SAFE_BUILTINS,
    }
    return namespace


def _dry_run_parallel_write_aliases(dry_writes: dict[str, Any]) -> dict[str, Any]:
    """The ``*_parallel`` write names for the dry-run namespace: recorder aliases.

    Dry-run writes are pure recorders with the sequential helpers' exact
    shapes, so the parallel names alias them: the identical script runs to
    completion and its would-be writes are recorded exactly as the sequential
    call would (per entity), with zero mutations either way.
    """
    return {
        "update_entities_parallel": dry_writes["update_entities"],
        "create_entities_parallel": dry_writes["create_entities"],
        "create_relationships_parallel": dry_writes["create_relationships"],
    }


def _dry_run_move_files_helper(records: list[dict[str, Any]]) -> Any:
    """Dry-run ``move_files_to_entity``: record the request, move nothing."""

    def move_files_to_entity(from_shared_ids: list[str], to_shared_id: str, language: str | None = None) -> dict:
        del language
        logger.info("dry-run record: move_files_to_entity x{} -> {}", len(from_shared_ids), to_shared_id)
        records.append(
            {
                "op": "move_files",
                "from_shared_ids": list(from_shared_ids),
                "to_shared_id": to_shared_id,
            }
        )
        return {"moved": len(from_shared_ids), "failed": 0, "skipped": 0}

    return move_files_to_entity


def _dry_run_move_files_parallel_helper(records: list[dict[str, Any]]) -> Any:
    """Dry-run ``move_files_to_entity_parallel``: one record per move, move nothing.

    Mirrors ``_dry_run_move_files_helper`` per move so ``_count_ops`` still
    counts every would-be move as ``would_rewire``, and keeps the real
    helper's :func:`assert_unique_move_targets` guard (a rehearsal of a
    duplicated-target script must fail exactly like the execute pass would).
    The per-move summaries carry the real helper's result shape, so the SAME
    script reads either mode's return values unchanged.
    """

    def move_files_to_entity_parallel(moves: list[dict], language: str | None = None) -> list[dict]:
        del language
        assert_unique_move_targets(moves)
        logger.info("dry-run record: move_files_to_entity_parallel x{} target(s)", len(moves))
        summaries: list[dict] = []
        for move in moves:
            records.append(
                {
                    "op": "move_files",
                    "from_shared_ids": list(move["from_shared_ids"]),
                    "to_shared_id": move["to_shared_id"],
                }
            )
            summaries.append(
                {
                    "to_shared_id": move["to_shared_id"],
                    "moved": len(move["from_shared_ids"]),
                    "failed": 0,
                    "skipped": 0,
                }
            )
        return summaries

    return move_files_to_entity_parallel


def _dry_run_query_entities_unwired() -> Any:
    """Unwired ``query_entities`` stub for the dry-run namespace (``entity_api is None``).

    Mirrors the loud-failure convention of the other real helpers' unwired
    stubs: raise a clear ``RuntimeError`` when the script actually calls it.
    """

    def query_entities_unwired(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(
            "query_entities requires a wired entity_api (got None). Wire "
            "EntityApiPort into the runtime to enable entity discovery reads."
        )

    return query_entities_unwired


def _dry_run_full_entities_unwired() -> Any:
    """Unwired ``query_entities_full`` stub for the dry-run namespace (``entity_api is None``).

    Same loud-failure convention as :func:`_dry_run_query_entities_unwired`:
    raise a clear ``RuntimeError`` when the script actually calls it.
    """

    def query_entities_full_unwired(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(
            "query_entities_full requires a wired entity_api (got None). Wire "
            "EntityApiPort into the runtime to enable entity discovery reads."
        )

    return query_entities_full_unwired


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
