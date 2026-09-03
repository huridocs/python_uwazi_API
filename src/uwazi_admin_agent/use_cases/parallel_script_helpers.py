"""The ``*_parallel`` bound helpers: auto-throttled bulk variants of the slow helpers.

Why these exist: the sequential bound helpers process a whole list in ONE
port call, and the ports loop PER ENTITY inside that call — one HTTP request
at a time. Against a remote production instance that serial HTTP is the
wall-clock cost of a bulk pass. These helpers split the list into batches and
run them through :class:`ParallelExecutor` on up to
``THROTTLE_MAX_WORKERS`` worker threads, reporting every batch's
:class:`BatchVerdict` to the shared :class:`ThrottleController`: Uwazi's load
complaints (``RATE_LIMITED`` results, 429 texts) back the allowance off
toward 1, clean streaks climb it back toward 4. Scripts never manage any of
it — the contract is "pass the whole list in one call".

What gets a parallel variant (per-entity HTTP loops inside the port):
- ``update_entities_parallel`` / ``create_entities_parallel`` /
  ``create_relationships_parallel`` — chunked port calls;
- ``get_entity_files_parallel`` / ``get_file_bytes_parallel`` — one fetch
  task per item (the raw repositories block per request, so the tasks run
  them on their own threads);
- ``move_files_to_entity_parallel`` — one task per TARGET (a merge's move
  step, its wall-clock cost): different targets are different Mongo rows, so
  their upload streams overlap safely, while ALL of one target's uploads
  stay sequential inside its task. :func:`assert_unique_move_targets`
  refuses a duplicated ``to_shared_id`` up front — two tasks writing the
  SAME row would race it (last save drops the other's file entry: a lost
  file).

What deliberately does NOT:
- ``delete_entities`` / publish-status — server-side BULK endpoints already
  (one request for the whole list); per-id traffic would risk limiter
  complaints for no gain.

Safety invariants preserved vs. the sequential path:
- update / rewire: ALL first-touch snapshots of the batch are taken BEFORE
  any of its writes applies (stronger than the sequential per-batch
  guarantee — an over-cap batch halts having written nothing, and a mid-batch
  crash still has every snapshot);
- create records new shared_ids after the batch, exactly like the sequential
  intercept;
- the cap, audit records, cache invalidation, and manifest writes all run
  on the script's thread after the pool joins — no new data races;
- write helpers keep the sequential helpers' exact list-of-dicts return
  shape and input order, so the SAME script runs in dummy / dry-run / real
  mode (the other namespaces bind these names with identical contracts);
- the parallel file-move keeps the sequential mover's best-effort file
  semantics (a missing-bytes/rejected upload counts as ``failed`` and never
  raises) while a hard port error still kills the script BEFORE the merge's
  deletes — sources keep their files (the slower-but-safe choice).

The write helpers take the :class:`BackupIntercept` typed ``Any`` — the same
decoupling :func:`build_real_exec_namespace` uses — and reach its private
backup seams (same package). Not unit-tested end-to-end (needs live ports);
the policy/classification/executor pieces are, and the flow is validated via
the simulation run like the rest of the intercept path.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from loguru import logger

from uwazi_admin_agent.configuration import PARALLEL_WRITE_BATCH_SIZE
from uwazi_admin_agent.domain.batch_outcome import BatchOutcome, BatchVerdict
from uwazi_admin_agent.domain.batch_split import split_batches
from uwazi_admin_agent.domain.file_restore import extract_file_refs
from uwazi_admin_agent.domain.snapshot import FileRef
from uwazi_admin_agent.domain.throttle_policy import classify_mutation_results, is_rate_limit_text, verdict_from_error_text
from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort
from uwazi_admin_agent.ports.file_repository_port import FileRepositoryPort
from uwazi_admin_agent.use_cases.parallel_executor import ParallelExecutor
from uwazi_agent.domain.agent_entity import AgentEntity
from uwazi_agent.domain.agent_entity_create import AgentEntityCreate
from uwazi_agent.domain.agent_relationship_create import AgentRelationshipCreate
from uwazi_agent.ports.entity_api_port import EntityApiPort
from uwazi_agent.ports.relationship_api_port import RelationshipApiPort
from uwazi_agent.use_cases.tools.python_code_executor import _ENTITY_READ_TOOLS


def build_parallel_write_helpers(
    entity_api: EntityApiPort,
    relationship_api: RelationshipApiPort | None,
    intercept: Any,
    tool_cache: Any,
    default_language: str,
    executor: ParallelExecutor,
) -> dict[str, Any]:
    """Build the 3 parallel WRITE helpers bound into the real exec namespace."""
    return {
        "update_entities_parallel": _update_parallel(entity_api, intercept, tool_cache, default_language, executor),
        "create_entities_parallel": _create_parallel(entity_api, intercept, tool_cache, default_language, executor),
        "create_relationships_parallel": _relationships_parallel(
            relationship_api, intercept, tool_cache, default_language, executor
        ),
    }


def build_parallel_read_helpers(
    entity_repository: EntityRepositoryPort | None,
    file_repository: FileRepositoryPort | None,
    default_language: str,
    executor: ParallelExecutor,
) -> dict[str, Any]:
    """Build the 2 parallel READ helpers (the real AND dry-run namespaces bind these)."""
    return {
        "get_entity_files_parallel": _entity_files_parallel(entity_repository, default_language, executor),
        "get_file_bytes_parallel": _file_bytes_parallel(file_repository, executor),
    }


def build_parallel_move_files_helper(
    entity_repository: EntityRepositoryPort | None,
    file_repository: FileRepositoryPort | None,
    default_language: str,
    executor: ParallelExecutor,
) -> dict[str, Any]:
    """Build the parallel file-move helper bound into the real exec namespace."""
    return {
        "move_files_to_entity_parallel": _move_files_parallel(entity_repository, file_repository, default_language, executor)
    }


# --- shared plumbing -----------------------------------------------------------


def _port_task(method: Callable[..., Any], /, *args: Any) -> Callable[[], Any]:
    """Wrap one async port call into a zero-arg task running on its own loop."""

    def task() -> Any:
        return asyncio.run(method(*args))

    return task


def _raise_first_write_error(errors: list[BaseException | None], executor: ParallelExecutor) -> None:
    """Record the complaint a raised write task implies, then re-raise it (script error)."""
    for exc in errors:
        if exc is not None:
            executor.record(verdict_from_error_text(str(exc)))
            raise exc


def _dump_write_results(values: list[Any | None]) -> list[dict[str, Any]]:
    """Flatten the per-chunk port results, in input order, to dumped dicts.

    A chunk that raised left ``None`` at its index — unreachable here because
    :func:`_raise_first_write_error` has already re-raised it, so the
    non-``None`` filter only satisfies the type checker, never drops data.
    """
    return [r.model_dump() for chunk in values if chunk is not None for r in chunk]


def _invalidate_entity_reads(tool_cache: Any) -> None:
    """Mirror the sequential CRUD helpers' post-write tool-cache invalidation."""
    if tool_cache is not None:
        tool_cache.invalidate_tools(_ENTITY_READ_TOOLS)


def _report_write_outcome(dumped: list[dict[str, Any]], executor: ParallelExecutor) -> BatchOutcome:
    """Classify a merged write-batch result and report its verdict to the throttle."""
    outcome = classify_mutation_results(dumped)
    executor.record(outcome.verdict)
    return outcome


def _log_write_batch(helper: str, outcome: BatchOutcome, count: int, started: float) -> None:
    logger.info(
        "script {}: {} entities, {} ok / {} failed / {} rate-limited ({:.1f}s)",
        helper,
        count,
        outcome.success_count,
        outcome.failure_count,
        outcome.rate_limited_count,
        time.monotonic() - started,
    )


def assert_unique_move_targets(moves: list[dict[str, Any]]) -> None:
    """Refuse two moves into the SAME target entity inside ONE call (lost-file race).

    Uploading a file to a Uwazi entity is a read-modify-write of that entity's
    Mongo row (load -> append to documents/attachments -> save): two
    concurrent upload streams into one ``to_shared_id`` race that row and the
    last save drops the other's file entry — the bytes reach storage but no
    row ever references them, a lost file. The parallel mover runs one task
    per target precisely so this cannot happen; a duplicated target inside
    one call would defeat it, so every namespace's
    ``move_files_to_entity_parallel`` refuses it up front (the real helper
    before any task is built, the recorders before any record is appended).
    Pure: no I/O.
    """
    seen: set[Any] = set()
    duplicated: list[Any] = []
    for move in moves:
        to_sid = move.get("to_shared_id")
        if to_sid in seen and to_sid not in duplicated:
            duplicated.append(to_sid)
        seen.add(to_sid)
    if duplicated:
        raise ValueError(
            "move_files_to_entity_parallel: each to_shared_id may appear in at most ONE move per "
            f"call; duplicated target(s): {duplicated}. Concurrent uploads into the same entity "
            "race its row and the last save drops the other's file entry - merge those sources "
            "into one move, or issue one call per target."
        )


# --- write helpers (real mode) --------------------------------------------------


def _update_parallel(
    entity_api: EntityApiPort,
    intercept: Any,
    tool_cache: Any,
    default_language: str,
    executor: ParallelExecutor,
) -> Callable[[list[dict], str | None], list[dict]]:
    def update_entities_parallel(entities_dicts: list[dict], language: str | None = None) -> list[dict]:
        lang = language or default_language
        entities = [AgentEntity(**e) for e in entities_dicts]  # validate BEFORE snapshotting
        if not entities:
            return []
        started = time.monotonic()
        ids = [e.shared_id for e in entities if e.shared_id]
        intercept._backup_before_modify(ids, lang)  # ALL snapshots precede ALL writes of the batch
        chunks = split_batches(entities, PARALLEL_WRITE_BATCH_SIZE)
        tasks = [_port_task(entity_api.update_entities, chunk, lang) for chunk in chunks]
        values, errors = executor.run(tasks)
        _raise_first_write_error(errors, executor)
        dumped = _dump_write_results(values)
        outcome = _report_write_outcome(dumped, executor)
        _invalidate_entity_reads(tool_cache)
        intercept._invalidate(ids)
        intercept._emit("update", ids)
        _log_write_batch("update_entities_parallel", outcome, len(ids), started)
        return dumped

    return update_entities_parallel


def _create_parallel(
    entity_api: EntityApiPort,
    intercept: Any,
    tool_cache: Any,
    default_language: str,
    executor: ParallelExecutor,
) -> Callable[[list[dict], str | None], list[dict]]:
    def create_entities_parallel(entities_dicts: list[dict], language: str | None = None) -> list[dict]:
        lang = language or default_language
        creates = [AgentEntityCreate(**e) for e in entities_dicts]
        if not creates:
            return []
        started = time.monotonic()
        chunks = split_batches(creates, PARALLEL_WRITE_BATCH_SIZE)
        tasks = [_port_task(entity_api.create_entities, chunk, lang) for chunk in chunks]
        values, errors = executor.run(tasks)
        _raise_first_write_error(errors, executor)
        dumped = _dump_write_results(values)
        outcome = _report_write_outcome(dumped, executor)
        _invalidate_entity_reads(tool_cache)
        intercept._record_created(dumped)  # post-create, exactly like the sequential intercept
        created_ids = [r["shared_id"] for r in dumped if r.get("success") and r.get("shared_id")]
        intercept._emit("create", created_ids)
        _log_write_batch("create_entities_parallel", outcome, len(creates), started)
        return dumped

    return create_entities_parallel


def _relationships_parallel(
    relationship_api: RelationshipApiPort | None,
    intercept: Any,
    tool_cache: Any,
    default_language: str,
    executor: ParallelExecutor,
) -> Callable[[list[dict], str | None], list[dict]]:
    def create_relationships_parallel(relationships_dicts: list[dict], language: str | None = None) -> list[dict]:
        if relationship_api is None:
            return [{"success": False, "error": "Relationship API not configured"}]
        lang = language or default_language
        rels = [AgentRelationshipCreate(**r) for r in relationships_dicts]
        if not rels:
            return []
        started = time.monotonic()
        from_ids = [r.from_entity_shared_id for r in rels]
        to_ids = [r.to_entity_shared_id for r in rels]
        intercept._backup_before_rewire(from_ids, lang)  # snapshots + rewired before ANY save applies
        chunks = split_batches(rels, PARALLEL_WRITE_BATCH_SIZE)
        tasks = [_port_task(relationship_api.create_relationships, chunk, lang) for chunk in chunks]
        values, errors = executor.run(tasks)
        _raise_first_write_error(errors, executor)
        dumped = _dump_write_results(values)
        outcome = _report_write_outcome(dumped, executor)
        _invalidate_entity_reads(tool_cache)
        intercept._invalidate(from_ids + to_ids)  # relations denormalize onto BOTH endpoints
        intercept._emit("create_relationships", from_ids)
        _log_write_batch("create_relationships_parallel", outcome, len(rels), started)
        return dumped

    return create_relationships_parallel


# --- file-move helper (real mode) ------------------------------------------------


def _move_files_parallel(
    entity_repository: EntityRepositoryPort | None,
    file_repository: FileRepositoryPort | None,
    default_language: str,
    executor: ParallelExecutor,
) -> Callable[[list[dict], str | None], list[dict]]:
    """Build the real ``move_files_to_entity_parallel`` (a merge's bulk file-move).

    One task per TARGET entity: uploads into different targets are
    read-modify-writes of different Mongo rows, so they overlap safely; ALL
    of one target's uploads stay SEQUENTIAL inside its task (concurrent
    uploads into one row race it — last save drops the other's file entry —
    which is why :func:`assert_unique_move_targets` refuses a duplicated
    target before any task is built). The per-target body mirrors the
    sequential mover (:func:`_build_move_files_real_helper`) file for file:
    fetch the source raw -> ``extract_file_refs`` -> fetch bytes -> re-upload,
    counting soft failures (missing bytes, rejected upload) as ``failed``
    without raising; a hard port EXCEPTION still propagates so the script
    dies BEFORE the merge's deletes — the sources keep their files.

    NOT intercept-decorated, like the sequential mover: a merge's target is
    already snapshotted as ``modified`` by ``update_entities`` and its
    sources (bytes captured) by ``delete_entities``. Unwired ports -> the
    read helpers' loud ``RuntimeError`` stub.
    """
    if entity_repository is None or file_repository is None:
        return _unwired_parallel(
            "move_files_to_entity_parallel",
            "entity_repository and file_repository",
            "Wire EntityRepositoryPort and FileRepositoryPort into the runtime/execute use case "
            "to enable parallel file-move for merges.",
        )

    def move_files_to_entity_parallel(moves: list[dict], language: str | None = None) -> list[dict]:
        assert_unique_move_targets(moves)
        if not moves:
            return []
        lang = language or default_language
        started = time.monotonic()
        tasks = [_move_target_task(entity_repository, file_repository, move, lang) for move in moves]
        values, errors = executor.run(tasks)
        _raise_first_write_error(errors, executor)
        # A raising task left None at its index — unreachable here (the line
        # above re-raised it); the filter only satisfies the type checker.
        results = [r for r in values if r is not None]
        moved = sum(r["moved"] for r in results)
        failed = sum(r["failed"] for r in results)
        # Uploads return only bool, so the move itself can never evidence a
        # load complaint (transient 429s are absorbed by the per-request
        # retry layer): every file landed -> CLEAN, any soft failure -> DEGRADED.
        executor.record(BatchVerdict.CLEAN if failed == 0 else BatchVerdict.DEGRADED)
        logger.info(
            "script move_files_to_entity_parallel: {} target(s), {} moved / {} failed ({:.1f}s)",
            len(results),
            moved,
            failed,
            time.monotonic() - started,
        )
        return results

    return move_files_to_entity_parallel


def _move_target_task(
    entity_repository: EntityRepositoryPort,
    file_repository: FileRepositoryPort,
    move: dict[str, Any],
    lang: str,
) -> Callable[[], dict[str, Any]]:
    """Bind one (move, lang) pair into a zero-arg task (no late-binding hazards)."""

    def task() -> dict[str, Any]:
        return _move_files_for_target(entity_repository, file_repository, move, lang)

    return task


def _move_files_for_target(
    entity_repository: EntityRepositoryPort,
    file_repository: FileRepositoryPort,
    move: dict[str, Any],
    lang: str,
) -> dict[str, Any]:
    """Move ONE move's source files to its target — sequentially (same-row safety).

    Runs on the task's own worker thread; every port call gets its own
    ``asyncio.run`` loop (the raw repositories block per request, so they
    must run on their own thread to overlap at all). The upload order is the
    sequential mover's: per source, documents first then attachments
    (``extract_file_refs`` already returns them in that order).
    """
    to_sid = move["to_shared_id"]
    moved = 0
    failed = 0
    for from_sid in move["from_shared_ids"]:
        raw = asyncio.run(entity_repository.get_raw_by_shared_id(from_sid, lang))
        for ref in extract_file_refs(raw):
            if _transfer_one_file(file_repository, ref, to_sid, lang):
                moved += 1
            else:
                failed += 1
    return {"to_shared_id": to_sid, "moved": moved, "failed": failed}


def _transfer_one_file(file_repository: FileRepositoryPort, ref: FileRef, to_sid: str, lang: str) -> bool:
    """Fetch one file's bytes and re-upload them to the target; False = not moved."""
    data = asyncio.run(file_repository.get_file_bytes(ref.filename))
    if data is None:
        return False
    upload_lang = ref.language or lang
    if ref.kind == "document":
        return asyncio.run(file_repository.upload_document(data, to_sid, upload_lang, ref.originalname, ref.content_type))
    return asyncio.run(file_repository.upload_attachment(data, to_sid, upload_lang, ref.originalname, ref.content_type))


# --- read helpers (real mode + dry-run bind the same factories) -------------------


def _entity_files_parallel(
    entity_repository: EntityRepositoryPort | None,
    default_language: str,
    executor: ParallelExecutor,
) -> Callable[[list[str], str | None], dict[str, list[dict]]]:
    if entity_repository is None:
        return _unwired_parallel(
            "get_entity_files_parallel",
            "entity_repository",
            "Wire EntityRepositoryPort into the runtime/execute use case to enable bulk supporting-file reads.",
        )

    def get_entity_files_parallel(shared_ids: list[str], language: str | None = None) -> dict[str, list[dict]]:
        lang = language or default_language
        if not shared_ids:
            return {}
        started = time.monotonic()

        def _fetch_files(sid: str) -> list[dict]:
            raw = asyncio.run(entity_repository.get_raw_by_shared_id(sid, lang))
            return [ref.model_dump() for ref in extract_file_refs(raw)]

        values, complaints = _run_reads_with_retry([_sid_task(_fetch_files, sid) for sid in shared_ids], executor)
        files = dict(zip(shared_ids, values, strict=True))
        logger.info(
            "script get_entity_files_parallel: {} entities, {} complaint(s) ({:.1f}s)",
            len(shared_ids),
            len(complaints),
            time.monotonic() - started,
        )
        return files

    return get_entity_files_parallel


def _file_bytes_parallel(
    file_repository: FileRepositoryPort | None,
    executor: ParallelExecutor,
) -> Callable[[list[str]], dict[str, bytes | None]]:
    if file_repository is None:
        return _unwired_parallel(
            "get_file_bytes_parallel",
            "file_repository",
            "Wire FileRepositoryPort into the runtime/execute use case to enable bulk file-byte reads.",
        )

    def get_file_bytes_parallel(filenames: list[str]) -> dict[str, bytes | None]:
        if not filenames:
            return {}
        started = time.monotonic()
        tasks = [_port_task(file_repository.get_file_bytes, name) for name in filenames]
        values, complaints = _run_reads_with_retry(tasks, executor)
        data = dict(zip(filenames, values, strict=True))
        logger.info(
            "script get_file_bytes_parallel: {} files, {} complaint(s) ({:.1f}s)",
            len(filenames),
            len(complaints),
            time.monotonic() - started,
        )
        return data

    return get_file_bytes_parallel


def _sid_task(fetch: Callable[[str], Any], sid: str) -> Callable[[], Any]:
    """Bind one (fetch, sid) pair into a zero-arg task (no late-binding hazards)."""

    def task() -> Any:
        return fetch(sid)

    return task


def _run_reads_with_retry(
    tasks: list[Callable[[], Any]],
    executor: ParallelExecutor,
) -> tuple[list[Any], list[str]]:
    """Run read tasks; retry each failure ONCE serially; report one batch verdict.

    Reads are idempotent, so a failed fetch is retried once at concurrency 1
    (the house "retry at most once after RATE_LIMITED" policy, extended to any
    transient read error). A task that fails the retry records its verdict and
    raises — the script fails exactly like a sequential fetch would. First
    attempts that needed a retry are returned as complaint texts: they feed the
    batch verdict (rate-limit markers -> ``RATE_LIMITED``, else ``DEGRADED``).
    The resolved list is rebuilt (not mutated) so its type carries no ``None``.
    """
    values, errors = executor.run(tasks)
    resolved: list[Any] = []
    complaints: list[str] = []
    for task, value, exc in zip(tasks, values, errors, strict=True):
        if exc is None:
            resolved.append(value)
            continue
        try:
            resolved.append(task())
        except Exception as retry_exc:  # noqa: BLE001 — a persistent read failure kills the batch, like the sequential path
            executor.record(verdict_from_error_text(str(retry_exc)))
            raise retry_exc
        complaints.append(str(exc))
    if any(is_rate_limit_text(text) for text in complaints):
        executor.record(BatchVerdict.RATE_LIMITED)
    elif complaints:
        executor.record(BatchVerdict.DEGRADED)
    else:
        executor.record(BatchVerdict.CLEAN)
    return resolved, complaints


def _unwired_parallel(name: str, port_name: str, guidance: str) -> Callable[..., Any]:
    """The loud-failure stub bound when a parallel read helper's port is ``None``."""

    def unwired(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(f"{name} requires a wired {port_name} (got None). {guidance}")

    return unwired
