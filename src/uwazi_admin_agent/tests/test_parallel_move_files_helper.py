"""Isolated unit tests for the ``move_files_to_entity_parallel`` bound helper.

Per AGENTS.md: no mocks/stubs, no network, no running Uwazi instance. The
namespaces are the REAL factories built with unwired ports (``None``) — the
established convention — plus the in-file ``_PassthroughIntercept`` real class
as the intercept seam stand-in (``test_parallel_script_helpers.py``
precedent); the real mover is driven against tiny REAL in-memory port classes
(the ``test_move_files_helper.py`` precedent). What is pinned:

- all three namespaces bind the same name with the same contract, so the
  SAME generated merge script runs in validation, dry-run, and execute;
- the single most important guard: a duplicated ``to_shared_id`` inside ONE
  call raises ``ValueError`` BEFORE any upload task runs / record is
  appended — two concurrent upload streams into one entity race its row and
  the last save drops the other's file entry (a lost file);
- per-target results in input order, per-move soft-failure counting, the
  CLEAN/DEGRADED batch verdicts, and the empty-input short-circuit;
- a hard port failure inside a task surfaces as a SCRIPT error: the run
  dies BEFORE the merge's deletes, so the failing source keeps its files.
"""

import asyncio
from typing import Any, override

import pytest

from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort
from uwazi_admin_agent.ports.file_repository_port import FileRepositoryPort
from uwazi_admin_agent.use_cases.dry_run_script_use_case import _count_ops
from uwazi_admin_agent.use_cases.script_exec_namespace import (
    ScopeViolationError,
    build_dry_run_namespace,
    build_exec_namespace,
    build_real_exec_namespace,
    run_script_sync,
)
from uwazi_admin_agent.use_cases.throttle_controller import ThrottleController


class _PassthroughIntercept:
    """Minimal real stand-in for the BackupIntercept seam (no ports involved)."""

    def decorate(self, crud: tuple) -> dict:
        names = (
            "create_entities",
            "update_entities",
            "delete_entities",
            "publish_entities",
            "unpublish_entities",
            "set_publish_status",
            "create_relationships",
        )
        return dict(zip(names, crud))

    def _backup_before_modify(self, shared_ids: list[str], language: str) -> None:
        pass

    def _backup_before_rewire(self, shared_ids: list[str], language: str) -> None:
        pass

    def _record_created(self, results: list) -> None:
        pass

    def _invalidate(self, shared_ids: list[str]) -> None:
        pass

    def _emit(self, op_kind: str, shared_ids: list[str], **kwargs: Any) -> None:
        pass


# --- in-memory ports (real classes, not mocks) ------------------------------


class InMemoryEntityRepo(EntityRepositoryPort):
    """Real in-memory raw store; optionally raises for one shared_id (hard-error path)."""

    def __init__(self, raws: dict[str, dict[str, Any]], explode_on: str | None = None) -> None:
        self._raws = raws
        self._explode_on = explode_on

    @override
    async def get_raw_by_shared_id(self, shared_id: str, language: str | None = None) -> dict[str, Any]:
        if shared_id == self._explode_on:
            raise RuntimeError(f"fetch failed for {shared_id}")
        return self._raws[shared_id]

    @override
    async def get_raw_by_internal_id(self, internal_id: str) -> dict[str, Any]:
        raise NotImplementedError

    @override
    async def save_raw(self, raw: dict[str, Any]) -> None:
        raise NotImplementedError

    @override
    async def create_raw(self, raw: dict[str, Any]) -> str:
        raise NotImplementedError

    @override
    async def delete_by_shared_id(self, shared_id: str) -> None:
        raise NotImplementedError


class InMemoryFileRepo(FileRepositoryPort):
    """Real in-memory byte store + upload recorder (best-effort upload switch)."""

    def __init__(self, bytes_store: dict[str, bytes], *, fail_uploads: bool = False) -> None:
        self._bytes = bytes_store
        self._fail = fail_uploads
        self.uploads: list[tuple[str, str, str | None, str, str]] = []  # kind, to_sid, lang, title, ctype

    @override
    async def get_file_bytes(self, filename: str) -> bytes | None:
        return self._bytes.get(filename)

    @override
    async def upload_document(
        self, data: bytes, shared_id: str, language: str | None, title: str, content_type: str
    ) -> bool:
        self.uploads.append(("document", shared_id, language, title, content_type))
        return not self._fail

    @override
    async def upload_attachment(
        self, data: bytes, shared_id: str, language: str | None, title: str, content_type: str
    ) -> bool:
        self.uploads.append(("attachment", shared_id, language, title, content_type))
        return not self._fail


def _raw(shared_id: str, *, documents: list | None = None, attachments: list | None = None) -> dict[str, Any]:
    return {
        "_id": "o-" + shared_id,
        "sharedId": shared_id,
        "title": "T",
        "language": "en",
        "documents": documents or [],
        "attachments": attachments or [],
    }


def _wired_repos() -> tuple[InMemoryEntityRepo, InMemoryFileRepo]:
    s1 = _raw(
        "S1",
        documents=[{"_id": "d1", "originalname": "a.pdf", "filename": "hashd1"}],
        attachments=[{"_id": "a1", "originalname": "b.txt", "filename": "hasha1"}],
    )
    s2 = _raw("S2", attachments=[{"_id": "a2", "originalname": "c.png", "filename": "hasha2"}])
    s3 = _raw("S3", documents=[{"_id": "d3", "originalname": "z.pdf", "filename": "hashd3"}])
    repo = InMemoryEntityRepo({"S1": s1, "S2": s2, "S3": s3})
    files = InMemoryFileRepo({"hashd1": b"PDF", "hasha1": b"TXT", "hasha2": b"PNG", "hashd3": b"PDF2"})
    return repo, files


def _dummy_namespace() -> dict:
    return build_exec_namespace(
        entity_api=None,
        relationship_api=None,
        loop=asyncio.new_event_loop(),
        scope={"S1", "S2", "T1", "T2"},
        dummy_entities=[],
        tool_cache=None,
        default_language="en",
    )


def _dry_run_namespace(records: list) -> dict:
    return build_dry_run_namespace(
        entity_api=None,
        loop=asyncio.new_event_loop(),
        file_repository=None,
        default_language="en",
        dry_run_records=records,
        entity_repository=None,
    )


def _real_namespace(
    entity_repository: InMemoryEntityRepo | None = None,
    file_repository: InMemoryFileRepo | None = None,
    throttle: ThrottleController | None = None,
) -> dict:
    """The real namespace with unwired CRUD ports (``entity_api=None`` — this
    file's convention; only the file-move path is exercised, which needs the
    repositories, not the entity API)."""
    return build_real_exec_namespace(
        entity_api=None,  # type: ignore[arg-type]
        relationship_api=None,
        loop=asyncio.new_event_loop(),
        intercept=_PassthroughIntercept(),
        tool_cache=None,
        default_language="en",
        entity_repository=entity_repository,
        file_repository=file_repository,
        throttle=throttle,
    )


# --- all three namespaces bind the same name --------------------------------


def test_all_three_namespaces_bind_the_parallel_move_name() -> None:
    assert "move_files_to_entity_parallel" in _dummy_namespace()
    assert "move_files_to_entity_parallel" in _dry_run_namespace([])
    assert "move_files_to_entity_parallel" in _real_namespace()


# --- dummy mode: scoped no-op ------------------------------------------------


def test_dummy_parallel_move_noops_in_scope_in_input_order() -> None:
    ns = _dummy_namespace()
    moves = [
        {"from_shared_ids": ["S1", "S2"], "to_shared_id": "T1"},
        {"from_shared_ids": ["S2"], "to_shared_id": "T2"},
    ]
    assert ns["move_files_to_entity_parallel"](moves) == [
        {"to_shared_id": "T1", "moved": 0, "failed": 0},
        {"to_shared_id": "T2", "moved": 0, "failed": 0},
    ]


def test_dummy_parallel_move_refuses_out_of_scope_ids() -> None:
    ns = _dummy_namespace()
    with pytest.raises(ScopeViolationError):
        ns["move_files_to_entity_parallel"]([{"from_shared_ids": ["REAL"], "to_shared_id": "T1"}])
    with pytest.raises(ScopeViolationError):
        ns["move_files_to_entity_parallel"]([{"from_shared_ids": ["S1"], "to_shared_id": "REAL"}])


def test_dummy_parallel_move_refuses_a_duplicated_target() -> None:
    ns = _dummy_namespace()
    moves = [
        {"from_shared_ids": ["S1"], "to_shared_id": "T1"},
        {"from_shared_ids": ["S2"], "to_shared_id": "T1"},
    ]
    with pytest.raises(ValueError, match="at most ONE move per call"):
        ns["move_files_to_entity_parallel"](moves)


# --- dry-run mode: one move_files record per move -----------------------------


def test_dry_run_parallel_move_records_one_op_per_move() -> None:
    records: list = []
    ns = _dry_run_namespace(records)
    moves = [
        {"from_shared_ids": ["S1", "S2"], "to_shared_id": "T1"},
        {"from_shared_ids": ["S3"], "to_shared_id": "T2"},
    ]
    summaries = ns["move_files_to_entity_parallel"](moves)
    assert summaries == [
        {"to_shared_id": "T1", "moved": 2, "failed": 0},
        {"to_shared_id": "T2", "moved": 1, "failed": 0},
    ]
    assert records == [
        {"op": "move_files", "from_shared_ids": ["S1", "S2"], "to_shared_id": "T1"},
        {"op": "move_files", "from_shared_ids": ["S3"], "to_shared_id": "T2"},
    ]
    # _count_ops feeds would_rewire from move_files records — one per move.
    assert _count_ops(records)["move_files"] == 2


def test_dry_run_parallel_move_refuses_a_duplicated_target_without_recording() -> None:
    records: list = []
    ns = _dry_run_namespace(records)
    moves = [
        {"from_shared_ids": ["S1"], "to_shared_id": "T1"},
        {"from_shared_ids": ["S2"], "to_shared_id": "T1"},
    ]
    with pytest.raises(ValueError, match="at most ONE move per call"):
        ns["move_files_to_entity_parallel"](moves)
    assert records == []  # the guard fired before any record was appended


# --- real mode: unwired ports surface as a script error -----------------------


def test_real_unwired_ports_raise_runtime_error_as_a_script_error() -> None:
    ns = _real_namespace()
    code = 'move_files_to_entity_parallel([{"from_shared_ids": ["S1"], "to_shared_id": "T1"}])'
    result, error = run_script_sync(code, ns)
    assert result is None
    assert error is not None
    assert "RuntimeError" in error
    assert "move_files_to_entity_parallel requires a wired" in error


# --- real mode: parallel across targets, sequential within one ----------------


def test_real_parallel_move_uploads_per_target_in_input_order() -> None:
    repo, files = _wired_repos()
    ns = _real_namespace(repo, files)
    moves = [
        {"from_shared_ids": ["S1", "S2"], "to_shared_id": "T1"},
        {"from_shared_ids": ["S3"], "to_shared_id": "T2"},
    ]
    summaries = ns["move_files_to_entity_parallel"](moves)
    assert summaries == [
        {"to_shared_id": "T1", "moved": 3, "failed": 0},
        {"to_shared_id": "T2", "moved": 1, "failed": 0},
    ]
    # Targets run in parallel so their uploads may interleave; each target's
    # own sequence must be the sequential mover's: per source, documents
    # first then attachments (extract_file_refs ordering).
    by_target: dict[str, list[tuple[str, str]]] = {}
    for kind, to_sid, lang, title, ctype in files.uploads:
        by_target.setdefault(to_sid, []).append((kind, title))
    assert by_target["T1"] == [("document", "a.pdf"), ("attachment", "b.txt"), ("attachment", "c.png")]
    assert by_target["T2"] == [("document", "z.pdf")]


def test_real_parallel_move_counts_soft_failures_and_reports_degraded() -> None:
    s1 = _raw("S1", documents=[{"_id": "d1", "originalname": "a.pdf", "filename": "gone"}])  # no bytes stored
    s2 = _raw("S2", attachments=[{"_id": "a2", "originalname": "c.png", "filename": "hasha2"}])
    repo = InMemoryEntityRepo({"S1": s1, "S2": s2})
    files = InMemoryFileRepo({"hasha2": b"PNG"}, fail_uploads=True)  # fetch ok, upload rejected
    throttle = ThrottleController()
    ns = _real_namespace(repo, files, throttle)
    moves = [
        {"from_shared_ids": ["S1"], "to_shared_id": "T1"},
        {"from_shared_ids": ["S2"], "to_shared_id": "T2"},
    ]
    summaries = ns["move_files_to_entity_parallel"](moves)
    assert summaries == [
        {"to_shared_id": "T1", "moved": 0, "failed": 1},
        {"to_shared_id": "T2", "moved": 0, "failed": 1},
    ]
    # DEGRADED resets the streak but never touches the worker allowance.
    snapshot = throttle.snapshot()
    assert snapshot.success_streak == 0
    assert snapshot.workers == 4


def test_real_clean_move_reports_clean_verdict() -> None:
    repo, files = _wired_repos()
    throttle = ThrottleController()
    ns = _real_namespace(repo, files, throttle)
    ns["move_files_to_entity_parallel"]([{"from_shared_ids": ["S1"], "to_shared_id": "T1"}])
    snapshot = throttle.snapshot()
    assert snapshot.success_streak == 1  # CLEAN grew the streak
    assert snapshot.workers == 4
    assert snapshot.complaint_count == 0


def test_real_duplicated_target_refuses_before_any_upload() -> None:
    repo, files = _wired_repos()
    ns = _real_namespace(repo, files)
    moves = [
        {"from_shared_ids": ["S1"], "to_shared_id": "T1"},
        {"from_shared_ids": ["S2"], "to_shared_id": "T1"},  # same target twice
    ]
    with pytest.raises(ValueError, match="at most ONE move per call"):
        ns["move_files_to_entity_parallel"](moves)
    assert files.uploads == []  # the guard fired BEFORE any task was built


def test_real_task_failure_surfaces_as_a_script_error() -> None:
    """A hard port EXCEPTION inside a task (not a soft None/False failure) must
    kill the script: the run dies BEFORE the merge's deletes, so the failing
    source keeps its files (the slower-but-safe choice)."""
    repo = InMemoryEntityRepo({"S1": _raw("S1")}, explode_on="BAD")
    files = InMemoryFileRepo({})
    ns = _real_namespace(repo, files)
    code = """
moves = [
    {"from_shared_ids": ["S1"], "to_shared_id": "T1"},
    {"from_shared_ids": ["BAD"], "to_shared_id": "T2"},
]
move_files_to_entity_parallel(moves)
result = "never-reached"
"""
    result, error = run_script_sync(code, ns)
    assert result is None
    assert error is not None
    assert "RuntimeError: fetch failed for BAD" in error


# --- empty input short-circuits in every mode ---------------------------------


def test_empty_moves_return_empty_list_in_every_mode() -> None:
    repo, files = _wired_repos()
    throttle = ThrottleController()
    real_ns = _real_namespace(repo, files, throttle)
    assert real_ns["move_files_to_entity_parallel"]([]) == []
    assert files.uploads == []
    assert throttle.snapshot().success_streak == 0  # empty input records no verdict
    assert _dummy_namespace()["move_files_to_entity_parallel"]([]) == []
    assert _dry_run_namespace([])["move_files_to_entity_parallel"]([]) == []
