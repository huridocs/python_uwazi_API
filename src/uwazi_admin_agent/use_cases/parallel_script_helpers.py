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
  them on their own threads).

What deliberately does NOT:
- ``delete_entities`` / publish-status — server-side BULK endpoints already
  (one request for the whole list); per-id traffic would risk limiter
  complaints for no gain;
- ``move_files_to_entity`` — every upload appends to the SAME target entity
  row, and concurrent uploads race that row (lost files). It stays
  sequential by design.

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
  mode (the other namespaces bind these names with identical contracts).

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
