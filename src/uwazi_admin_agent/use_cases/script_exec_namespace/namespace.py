"""Sandbox namespace module split out of ``script_exec_namespace`` (behavior preserved)."""

from __future__ import annotations

import asyncio
import contextlib
import io
import traceback
from typing import Any, Callable

from uwazi_admin_agent.domain.llm_budget import LlmLedger
from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort
from uwazi_admin_agent.ports.file_repository_port import FileRepositoryPort
from uwazi_admin_agent.ports.llm_runtime_port import LlmRuntimePort
from uwazi_admin_agent.ports.segmentation_repository_port import SegmentationRepositoryPort
from uwazi_admin_agent.ports.text_cache_port import TextCachePort
from uwazi_admin_agent.ports.translation_port import TranslationPort
from uwazi_admin_agent.ports.url_fetcher_port import UrlFetcherPort
from uwazi_admin_agent.use_cases.llm_script_helpers import build_llm_script_helpers, selection_llm_refusal
from uwazi_admin_agent.use_cases.parallel_executor import ParallelExecutor
from uwazi_admin_agent.use_cases.parallel_script_helpers import (
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
from uwazi_agent.ports.entity_api_port import EntityApiPort
from uwazi_agent.ports.relationship_api_port import RelationshipApiPort
from uwazi_agent.use_cases.tools.python_code_executor import _build_sync_crud_functions

from .constants import _STDLIB, SAFE_BUILTINS
from .dry_run import (
    _dry_run_full_entities_unwired,
    _dry_run_move_files_helper,
    _dry_run_move_files_parallel_helper,
    _dry_run_parallel_write_aliases,
    _dry_run_query_entities_unwired,
    _dry_run_write_helpers,
    _noop_add_info,
    _noop_record_skipped,
)
from .files import (
    _build_get_entity_files_real_helper,
    _build_get_file_bytes_real_helper,
    _build_get_segmentation_real_helper,
    _build_get_url_text_real_helper,
    _build_move_files_real_helper,
    _build_translate_text_real_helper,
    _dummy_parallel_helpers,
    _get_entity_files_noop_scoped,
    _get_file_bytes_noop,
    _get_segmentation_noop,
    _get_url_text_noop,
    _move_files_noop_scoped,
    _scoped_write_helpers,
    _translate_text_noop,
)
from .reads import (
    _real_full_entities_factory,
    _real_sync_query_entities_factory,
    _sync_full_entities_factory,
    _sync_query_entities_factory,
)
from .scope import _scoped_entity_reads


def build_exec_namespace(
    entity_api: Any,
    relationship_api: Any,
    loop: asyncio.AbstractEventLoop,
    scope: set[str],
    dummy_entities: list[AgentEntity],
    tool_cache: Any,
    default_language: str,
    target_shared_id: str | None = None,
) -> dict[str, Any]:
    """Construct the dummy-scoped exec namespace for the candidate script.

    ``scope`` is the **live** set of dummy shared_ids: created dummies are added
    to it by the scoped ``create_entities`` so cleanup can delete them. The
    reused sync CRUD helpers (from ``_build_sync_crud_functions``) are wrapped to
    refuse out-of-scope ids. The bound stdlib subset and ``SAFE_BUILTINS`` make
    the namespace the §2.1 safety boundary. Returns the namespace dict ready for
    ``exec``; the caller reads ``namespace["result"]`` afterwards. When
    ``target_shared_id`` is given it is bound as ``TARGET_SHARED_ID`` (the apply
    script's one-entity contract, exercised by the validation harness); the
    default ``None`` still binds the name (create mode reads None).
    """
    crud = _build_sync_crud_functions(entity_api, relationship_api, default_language, loop, tool_cache)
    scoped = _scoped_write_helpers(crud, scope)
    namespace: dict[str, Any] = {
        "query_entities": _sync_query_entities_factory(dummy_entities, scope),
        "query_entities_full": _sync_full_entities_factory(dummy_entities, scope),
        **scoped,
        **_dummy_parallel_helpers(scoped, scope),
        "add_info": _noop_add_info,
        "record_skipped": _noop_record_skipped,
        "move_files_to_entity": _move_files_noop_scoped(scope),
        "get_entity_files": _get_entity_files_noop_scoped(scope),
        "get_file_bytes": _get_file_bytes_noop(),
        "get_segmentation": _get_segmentation_noop(),
        "get_url_text": _get_url_text_noop(),
        "translate_text": _translate_text_noop(),
        **_STDLIB,
        "__builtins__": SAFE_BUILTINS,
    }
    # The APPLY script's contract, exercised by the validation harness: bind
    # the one-entity target so a candidate apply script is validated under the
    # same binding the outcome pass / apply use case uses. The dummy harness
    # passes the first dummy's shared_id (or None for a create-mode script).
    namespace["TARGET_SHARED_ID"] = target_shared_id
    return namespace


def build_real_exec_namespace(
    entity_api: EntityApiPort,
    relationship_api: RelationshipApiPort | None,
    loop: asyncio.AbstractEventLoop,
    intercept: Any,
    tool_cache: Any,
    default_language: str,
    entity_repository: EntityRepositoryPort | None = None,
    file_repository: FileRepositoryPort | None = None,
    segmentation_repository: SegmentationRepositoryPort | None = None,
    url_fetcher: UrlFetcherPort | None = None,
    translation_port: TranslationPort | None = None,
    throttle: ThrottleController | None = None,
    llm_runtime: LlmRuntimePort | None = None,
    llm_ledger: LlmLedger | None = None,
    llm_cache: TextCachePort | None = None,
    target_shared_id: str | None = None,
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
      a merge's target is snapshotted as ``modified`` by ``update_entities`` when
      the script writes it, and the sources as ``deleted`` (their files captured)
      by ``delete_entities``. A file-only merge writes nothing to its target, so
      there is no target snapshot at all; either way the moved copies are
      removed by the revert's merge-copy cleanup (verified against the sources'
      captured bytes), which is why the copies do not need to ride a snapshot.
      If either port is None a stub is bound that raises when the script calls
      it.
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
    - ``TARGET_SHARED_ID`` (the apply script's contract): when a sharedId string
      is given every discovery helper is post-filtered to that ONE entity (the
      apply path); ``None`` binds ``TARGET_SHARED_ID = None`` (create mode).
    """
    crud = _build_sync_crud_functions(entity_api, relationship_api, default_language, loop, tool_cache)
    controller = throttle if throttle is not None else ThrottleController()
    executor = ParallelExecutor(controller)
    namespace: dict[str, Any] = {
        "query_entities": _real_sync_query_entities_factory(entity_api, loop),
        "query_entities_full": _real_full_entities_factory(entity_api, loop),
        **intercept.decorate(crud),
        **build_parallel_write_helpers(entity_api, relationship_api, intercept, tool_cache, default_language, executor),
        "add_info": _noop_add_info,
        "record_skipped": _noop_record_skipped,
        "move_files_to_entity": _build_move_files_real_helper(entity_repository, file_repository, loop, default_language),
        **build_parallel_move_files_helper(entity_repository, file_repository, default_language, executor),
        **build_parallel_file_cleanup_helper(entity_repository, file_repository, intercept, default_language, executor),
        **build_parallel_file_delete_helper(entity_repository, file_repository, intercept, default_language, executor),
        "get_entity_files": _build_get_entity_files_real_helper(entity_repository, loop, default_language),
        "get_file_bytes": _build_get_file_bytes_real_helper(file_repository, loop),
        "get_segmentation": _build_get_segmentation_real_helper(segmentation_repository, loop, default_language),
        "get_url_text": _build_get_url_text_real_helper(url_fetcher, loop),
        "translate_text": _build_translate_text_real_helper(translation_port, loop),
        **build_parallel_read_helpers(
            entity_repository, file_repository, default_language, executor, segmentation_repository
        ),
        **build_llm_script_helpers(llm_runtime, llm_ledger, loop, text_cache=llm_cache),
        **_STDLIB,
        "__builtins__": SAFE_BUILTINS,
    }
    if target_shared_id is not None:
        _scoped_entity_reads(namespace, {target_shared_id})
    namespace["TARGET_SHARED_ID"] = target_shared_id
    return namespace


def build_dry_run_namespace(
    entity_api: EntityApiPort | None,
    loop: asyncio.AbstractEventLoop,
    file_repository: FileRepositoryPort | None,
    default_language: str,
    dry_run_records: list[dict[str, Any]],
    entity_repository: EntityRepositoryPort | None = None,
    segmentation_repository: SegmentationRepositoryPort | None = None,
    url_fetcher: UrlFetcherPort | None = None,
    translation_port: TranslationPort | None = None,
    throttle: ThrottleController | None = None,
    llm_runtime: LlmRuntimePort | None = None,
    llm_ledger: LlmLedger | None = None,
    llm_progress: Callable[[int], None] | None = None,
    llm_cache: TextCachePort | None = None,
    entity_scope: set[str] | None = None,
    target_shared_id: str | None = None,
    apply_mode: bool = False,
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
      appending into ``dry_run_records`` — no write ever reaches Uwazi, so the
      dry run can never mutate it. The move recorders additionally READ each
      source's file refs (best-effort) so the review can show which primary
      documents the merge would copy; a failed read leaves that source's list
      empty. The ``*_parallel`` write names alias the same
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
    - ``TARGET_SHARED_ID``: when ``target_shared_id`` is a sharedId string the
      APPLY script runs against that ONE entity — every discovery helper is
      post-filtered to it (see :func:`_scoped_entity_reads`) so reads/writes can
      only ever touch that entity. ``None`` = the selection (discovery) mode:
      ``TARGET_SHARED_ID`` is bound as ``None`` and nothing is scoped.
    - LLM helpers: bound ONLY when BOTH ``llm_runtime`` and ``llm_ledger`` are
      given AND ``apply_mode`` is True — a budgeted pass that lets the script's
      own ``llm_complete``/``llm_map`` calls through (the planning pass wires
      the creation-time budget this way). Without them the helpers are
      :func:`selection_llm_refusal` loud stubs — right for an LLM-free pass,
      and the designed failure for one that needs the LLM without a budget.
    """
    controller = throttle if throttle is not None else ThrottleController()
    executor = ParallelExecutor(controller)
    dry_writes = _dry_run_write_helpers(dry_run_records)
    if apply_mode and llm_runtime is not None and llm_ledger is not None:
        llm_helpers = build_llm_script_helpers(llm_runtime, llm_ledger, loop, progress=llm_progress, text_cache=llm_cache)
    else:
        # Selection mode (or an apply run without budget wiring): loud refusals.
        llm_helpers = selection_llm_refusal()
    namespace: dict[str, Any] = {
        "query_entities": (
            _dry_run_query_entities_unwired() if entity_api is None else _real_sync_query_entities_factory(entity_api, loop)
        ),
        "query_entities_full": (
            _dry_run_full_entities_unwired() if entity_api is None else _real_full_entities_factory(entity_api, loop)
        ),
        **dry_writes,
        **_dry_run_parallel_write_aliases(dry_writes),
        "move_files_to_entity": _dry_run_move_files_helper(dry_run_records, entity_repository, loop, default_language),
        "move_files_to_entity_parallel": _dry_run_move_files_parallel_helper(
            dry_run_records, entity_repository, loop, default_language
        ),
        **build_parallel_file_cleanup_dry_run_helper(
            entity_repository, file_repository, default_language, executor, dry_run_records
        ),
        **build_parallel_file_delete_dry_run_helper(
            entity_repository, file_repository, default_language, executor, dry_run_records
        ),
        "get_entity_files": _build_get_entity_files_real_helper(entity_repository, loop, default_language),
        "get_file_bytes": _build_get_file_bytes_real_helper(file_repository, loop),
        "get_segmentation": _build_get_segmentation_real_helper(segmentation_repository, loop, default_language),
        "get_url_text": _build_get_url_text_real_helper(url_fetcher, loop),
        "translate_text": _build_translate_text_real_helper(translation_port, loop),
        **build_parallel_read_helpers(
            entity_repository, file_repository, default_language, executor, segmentation_repository
        ),
        **llm_helpers,
        **_STDLIB,
        "__builtins__": SAFE_BUILTINS,
    }
    if entity_scope is not None:
        _scoped_entity_reads(namespace, set(entity_scope))
    if target_shared_id is not None:
        _scoped_entity_reads(namespace, {target_shared_id})
    namespace["TARGET_SHARED_ID"] = target_shared_id
    return namespace


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


@contextlib.contextmanager
def _captured_stdout():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield buf
