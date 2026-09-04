"""Isolated unit tests for the ``dedupe_entity_files_parallel`` bound helper.

Per AGENTS.md: no mocks/stubs, no network, no running Uwazi instance. The
namespaces are the REAL factories; the real helper is driven against tiny
REAL in-memory port classes (the ``test_parallel_move_files_helper.py``
precedent — subclass the ports, implement every abstract method, record
deletes in a plain list) plus the in-file ``_PassthroughIntercept`` real class
as the intercept seam stand-in. What is pinned:

- all three namespaces bind the same name with the same contract, so the SAME
  generated cleanup script runs in validation, dry-run, and execute;
- the no-loss rules end-to-end: only byte-identical duplicates are deleted
  (the keeper is the first copy in raw order), unfetchable bytes are kept,
  same-named-but-different content is kept, and a connection-cited redundant
  copy is kept (``kept_cited``) because Uwazi tears down connections citing a
  deleted file;
- the duplicated-shared_id ValueError guard fires BEFORE any delete/record
  in every mode (two tasks on one entity would race the same file rows);
- a soft ``False`` delete counts as ``failed`` + DEGRADED (never raises); a
  hard port exception kills the script loudly (the ``_raise_first_write_error``
  pattern);
- the dry run RECORDS every would-be delete (op ``delete_file`` with
  shared_id/file_id/originalname/filename) — the audit trail for an operation
  that is not revertable — and never records a kept copy;
- ``_count_ops``/``DryRunReport``/``_format_result`` surface the new counter.
"""

import asyncio
from typing import Any, override

import pytest

from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort
from uwazi_admin_agent.ports.file_repository_port import FileRepositoryPort
from uwazi_admin_agent.use_cases.dry_run_script_use_case import DryRunReport, _count_ops
from uwazi_admin_agent.use_cases.run_dry_run_script_tool import _format_result
from uwazi_admin_agent.use_cases.script_exec_namespace import (
    ScopeViolationError,
    build_dry_run_namespace,
    build_exec_namespace,
    build_real_exec_namespace,
    run_script_sync,
)
from uwazi_admin_agent.use_cases.throttle_controller import ThrottleController


class _PassthroughIntercept:
    """Real stand-in for the BackupIntercept seam (no ports involved).

    Records the two file-deletion seams so the tests can assert the
    backup-before-delete ordering and the manifest recording, exactly like
    the real intercept would (plain dict/list bookkeeping, no mocks):
    - ``backups`` maps (shared_id, file_id) -> persisted bytes;
    - ``events`` is the ordered backup/delete trace the ordering test reads;
    - ``deleted_file_records`` collects the recorded :class:`DeletedFile` entries.
    """

    def __init__(self) -> None:
        self.backups: dict[tuple[str, str], bytes] = {}
        self.events: list[tuple[str, str, str]] = []  # (kind, shared_id, file_id)
        self.deleted_file_records: list[Any] = []

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

    def _save_file_backup(self, shared_id: str, file_id: str, data: bytes) -> None:
        self.backups[(shared_id, file_id)] = data
        self.events.append(("backup", shared_id, file_id))

    def _record_deleted_files(self, records: list) -> None:
        self.deleted_file_records.extend(records)


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


class InMemoryCleanupFileRepo(FileRepositoryPort):
    """Real in-memory byte store + delete recorder (best-effort delete switch)."""

    def __init__(self, bytes_store: dict[str, bytes], *, fail_deletes: bool = False) -> None:
        self._bytes = bytes_store
        self._fail = fail_deletes
        self.deletes: list[str] = []  # file_ids delete_file was called with

    @override
    async def get_file_bytes(self, filename: str) -> bytes | None:
        return self._bytes.get(filename)

    @override
    async def upload_document(
        self, data: bytes, shared_id: str, language: str | None, title: str, content_type: str
    ) -> bool:
        raise NotImplementedError

    @override
    async def upload_attachment(
        self, data: bytes, shared_id: str, language: str | None, title: str, content_type: str
    ) -> bool:
        raise NotImplementedError

    @override
    async def delete_file(self, file_id: str) -> bool:
        self.deletes.append(file_id)
        return not self._fail


def _raw(
    shared_id: str,
    *,
    documents: list | None = None,
    attachments: list | None = None,
    relations: list | None = None,
) -> dict[str, Any]:
    return {
        "_id": "o-" + shared_id,
        "sharedId": shared_id,
        "title": "T",
        "language": "en",
        "documents": documents or [],
        "attachments": attachments or [],
        "relations": relations or [],
    }


_INCIDENT_FILES: dict[str, bytes] = {
    "f1": b"SPANISH",
    "f2": b"SPANISH",
    "f3": b"SPANISH",
    "f4": b"ENGLISH",
    "g1": b"HTML",
    "g2": b"HTML",
    "u1": b"PDF",
}


def _incident_repos(*, fail_deletes: bool = False) -> tuple[InMemoryEntityRepo, InMemoryCleanupFileRepo]:
    """The live incident's shape: a merged target carrying 3 byte-identical
    Spanish copies + 1 genuine English translation of the same document name,
    2 byte-identical HTML attachments, one unique PDF — and a connection
    citing one of the redundant Spanish copies."""
    e1 = _raw(
        "E1",
        documents=[
            {"_id": "d1", "originalname": "a.pdf", "filename": "f1", "size": 7},
            {"_id": "d2", "originalname": "a.pdf", "filename": "f2", "size": 7},
            {"_id": "d3", "originalname": "a.pdf", "filename": "f3", "size": 7},
            {"_id": "d4", "originalname": "a.pdf", "filename": "f4", "size": 7},
        ],
        attachments=[
            {"_id": "h1", "originalname": "doc.html", "filename": "g1", "size": 4},
            {"_id": "h2", "originalname": "doc.html", "filename": "g2", "size": 4},
        ],
        relations=[{"entity": "E1", "file": "d3"}],  # a text reference cites the d3 copy
    )
    e2 = _raw("E2", documents=[{"_id": "x1", "originalname": "unique.pdf", "filename": "u1", "size": 3}])
    repo = InMemoryEntityRepo({"E1": e1, "E2": e2})
    files = InMemoryCleanupFileRepo(dict(_INCIDENT_FILES), fail_deletes=fail_deletes)
    return repo, files


def _dummy_namespace() -> dict:
    return build_exec_namespace(
        entity_api=None,
        relationship_api=None,
        loop=asyncio.new_event_loop(),
        scope={"E1", "E2"},
        dummy_entities=[],
        tool_cache=None,
        default_language="en",
    )


def _dry_run_namespace(
    records: list,
    entity_repository: InMemoryEntityRepo | None = None,
    file_repository: InMemoryCleanupFileRepo | None = None,
) -> dict:
    return build_dry_run_namespace(
        entity_api=None,
        loop=asyncio.new_event_loop(),
        file_repository=file_repository,
        default_language="en",
        dry_run_records=records,
        entity_repository=entity_repository,
    )


def _real_namespace(
    entity_repository: InMemoryEntityRepo | None = None,
    file_repository: InMemoryCleanupFileRepo | None = None,
    throttle: ThrottleController | None = None,
    intercept: _PassthroughIntercept | None = None,
) -> dict:
    """The real namespace with unwired CRUD ports (this file's convention; only
    the file-cleanup path is exercised, which needs the repositories)."""
    return build_real_exec_namespace(
        entity_api=None,  # type: ignore[arg-type]
        relationship_api=None,
        loop=asyncio.new_event_loop(),
        intercept=intercept if intercept is not None else _PassthroughIntercept(),
        tool_cache=None,
        default_language="en",
        entity_repository=entity_repository,
        file_repository=file_repository,
        throttle=throttle,
    )


# --- all three namespaces bind the same name --------------------------------


def test_all_three_namespaces_bind_the_dedupe_name() -> None:
    assert "dedupe_entity_files_parallel" in _dummy_namespace()
    assert "dedupe_entity_files_parallel" in _dry_run_namespace([])
    assert "dedupe_entity_files_parallel" in _real_namespace()


# --- dummy mode: scoped no-op ------------------------------------------------


def test_dummy_dedupe_noops_in_scope_in_input_order() -> None:
    ns = _dummy_namespace()
    summaries = ns["dedupe_entity_files_parallel"](["E1", "E2"])
    assert summaries == [
        {"shared_id": "E1", "duplicates": 0, "deleted": 0, "failed": 0, "kept_cited": 0},
        {"shared_id": "E2", "duplicates": 0, "deleted": 0, "failed": 0, "kept_cited": 0},
    ]


def test_dummy_dedupe_refuses_out_of_scope_ids() -> None:
    ns = _dummy_namespace()
    with pytest.raises(ScopeViolationError):
        ns["dedupe_entity_files_parallel"](["REAL"])


def test_dummy_dedupe_refuses_a_duplicated_shared_id() -> None:
    ns = _dummy_namespace()
    with pytest.raises(ValueError, match="at most once per call"):
        ns["dedupe_entity_files_parallel"](["E1", "E1"])


# --- dry-run mode: real discovery reads, recorded deletes ---------------------


def test_dry_run_dedupe_records_would_be_deletes_not_kept_copies() -> None:
    """The audit trail: one delete_file record per WOULD-BE delete (with
    shared_id/file_id/originalname/filename for review), none for the keeper,
    the English original, or the connection-cited copy that is kept."""
    records: list = []
    repo, files = _incident_repos()
    ns = _dry_run_namespace(records, repo, files)
    summaries = ns["dedupe_entity_files_parallel"](["E1", "E2"])
    assert summaries == [
        {"shared_id": "E1", "duplicates": 3, "deleted": 2, "failed": 0, "kept_cited": 1},
        {"shared_id": "E2", "duplicates": 0, "deleted": 0, "failed": 0, "kept_cited": 0},
    ]
    assert records == [
        {"op": "delete_file", "shared_id": "E1", "file_id": "d2", "originalname": "a.pdf", "filename": "f2"},
        {"op": "delete_file", "shared_id": "E1", "file_id": "h2", "originalname": "doc.html", "filename": "g2"},
    ]
    assert files.deletes == []  # the dry run never deletes


def test_dry_run_dedupe_refuses_duplicated_shared_id_without_recording() -> None:
    records: list = []
    repo, files = _incident_repos()
    ns = _dry_run_namespace(records, repo, files)
    with pytest.raises(ValueError, match="at most once per call"):
        ns["dedupe_entity_files_parallel"](["E1", "E1"])
    assert records == []  # the guard fired before any discovery/record


def test_dry_run_dedupe_unwired_ports_raise_runtime_error() -> None:
    ns = _dry_run_namespace([])
    with pytest.raises(RuntimeError, match="dedupe_entity_files_parallel requires a wired"):
        ns["dedupe_entity_files_parallel"](["E1"])


def test_dry_run_dedupe_empty_returns_empty() -> None:
    """Empty input short-circuits before any discovery — BUT only with wired
    ports: the unwired stub raises on ANY call (the move helper's stub
    convention, shared so an unwired script fails identically in every mode)."""
    records: list = []
    repo, files = _incident_repos()
    ns = _dry_run_namespace(records, repo, files)
    assert ns["dedupe_entity_files_parallel"]([]) == []
    assert records == []
    assert files.deletes == []


def test_count_ops_and_report_surface_would_delete_files() -> None:
    """delete_file records feed the would_delete_files counter (distinct from
    would_delete entities), and _format_result renders it for review."""
    records = [
        {"op": "delete", "shared_id": "E9"},
        {"op": "delete_file", "shared_id": "E1", "file_id": "d2", "originalname": "a.pdf", "filename": "f2"},
        {"op": "delete_file", "shared_id": "E1", "file_id": "h2", "originalname": "doc.html", "filename": "g2"},
    ]
    counts = _count_ops(records)
    assert counts["delete"] == 1
    assert counts["delete_file"] == 2
    report = DryRunReport(
        passed=True,
        script_result="cleaned",
        script_error=None,
        would_create=0,
        would_update=0,
        would_delete=1,
        would_publish=0,
        would_unpublish=0,
        would_rewire=0,
        would_delete_files=counts["delete_file"],
        records=records,
    )
    assert report.would_delete_files == 2
    out = _format_result(report, 1, 3)
    assert "delete_files=2" in out
    assert "No-op warning" not in out  # file deletes count as would-be writes


# --- real mode: unwired ports surface as a script error -----------------------


def test_real_unwired_ports_raise_runtime_error_as_a_script_error() -> None:
    ns = _real_namespace()
    code = 'dedupe_entity_files_parallel(["E1"])'
    result, error = run_script_sync(code, ns)
    assert result is None
    assert error is not None
    assert "RuntimeError" in error
    assert "dedupe_entity_files_parallel requires a wired" in error


# --- real mode: the no-loss rules end-to-end ----------------------------------


def test_real_dedupe_deletes_only_redundant_byte_identical_copies() -> None:
    """THE incident, end-to-end: 3 byte-identical Spanish copies collapse to
    the keeper (the connection-cited copy stays too), the genuine English
    translation is kept, the multiplied HTML attachment collapses to one."""
    repo, files = _incident_repos()
    throttle = ThrottleController()
    ns = _real_namespace(repo, files, throttle)
    summaries = ns["dedupe_entity_files_parallel"](["E1", "E2"])
    assert summaries == [
        {"shared_id": "E1", "duplicates": 3, "deleted": 2, "failed": 0, "kept_cited": 1},
        {"shared_id": "E2", "duplicates": 0, "deleted": 0, "failed": 0, "kept_cited": 0},
    ]
    # Deleted: the SECOND Spanish copy (d2) and the SECOND html (h2) — never
    # the keeper (d1), the cited copy (d3), the English original (d4), or the
    # unique PDF (x1).
    assert files.deletes == ["d2", "h2"]
    snapshot = throttle.snapshot()
    assert snapshot.success_streak == 1  # CLEAN grew the streak
    assert snapshot.workers == 4
    assert snapshot.complaint_count == 0


def test_real_dedupe_keeps_unfetchable_and_same_named_different_content() -> None:
    e1 = _raw(
        "E1",
        documents=[
            {"_id": "d1", "originalname": "a.pdf", "filename": "f1", "size": 3},
            {"_id": "d2", "originalname": "a.pdf", "filename": "gone", "size": 3},  # bytes unfetchable
            {"_id": "d3", "originalname": "a.pdf", "filename": "f3", "size": 3},  # same name, different bytes
        ],
    )
    repo = InMemoryEntityRepo({"E1": e1})
    files = InMemoryCleanupFileRepo({"f1": b"AAA", "f3": b"BBB"})  # "gone" -> None
    ns = _real_namespace(repo, files)
    summaries = ns["dedupe_entity_files_parallel"](["E1"])
    assert summaries == [{"shared_id": "E1", "duplicates": 0, "deleted": 0, "failed": 0, "kept_cited": 0}]
    assert files.deletes == []  # identity unconfirmed / not identical -> never deleted


def test_real_dedupe_never_deletes_the_last_copy_of_anything() -> None:
    e1 = _raw("E1", documents=[{"_id": "d1", "originalname": "a.pdf", "filename": "f1", "size": 3}])
    e2 = _raw("E2")  # no files at all
    repo = InMemoryEntityRepo({"E1": e1, "E2": e2})
    files = InMemoryCleanupFileRepo({"f1": b"AAA"})
    ns = _real_namespace(repo, files)
    summaries = ns["dedupe_entity_files_parallel"](["E1", "E2"])
    assert summaries == [
        {"shared_id": "E1", "duplicates": 0, "deleted": 0, "failed": 0, "kept_cited": 0},
        {"shared_id": "E2", "duplicates": 0, "deleted": 0, "failed": 0, "kept_cited": 0},
    ]
    assert files.deletes == []


def test_real_dedupe_counts_failed_deletes_and_reports_degraded() -> None:
    """The server refuses every delete (the soft bool-False path): each refusal
    counts as ``failed``, the keeper/cited copies still stand, and the batch
    verdict is DEGRADED (never an exception, never RATE_LIMITED)."""
    repo, refusing = _incident_repos(fail_deletes=True)
    throttle = ThrottleController()
    ns = _real_namespace(repo, refusing, throttle)
    summaries = ns["dedupe_entity_files_parallel"](["E1"])
    assert summaries == [{"shared_id": "E1", "duplicates": 3, "deleted": 0, "failed": 2, "kept_cited": 1}]
    assert refusing.deletes == ["d2", "h2"]  # both attempted, both refused
    # DEGRADED resets the streak but never touches the worker allowance.
    snapshot = throttle.snapshot()
    assert snapshot.success_streak == 0
    assert snapshot.workers == 4


def test_real_task_failure_surfaces_as_a_script_error() -> None:
    """A hard port EXCEPTION inside a task (not a soft None/False failure) must
    kill the script BEFORE any further deletes (the slower-but-safe choice)."""
    repo = InMemoryEntityRepo({"E1": _raw("E1"), "E2": _raw("E2")}, explode_on="BAD")
    files = InMemoryCleanupFileRepo({})
    ns = _real_namespace(repo, files)
    code = """
dedupe_entity_files_parallel(["E1", "BAD"])
result = "never-reached"
"""
    result, error = run_script_sync(code, ns)
    assert result is None
    assert error is not None
    assert "RuntimeError: fetch failed for BAD" in error


def test_real_dedupe_refuses_duplicated_shared_id_before_any_delete() -> None:
    repo, files = _incident_repos()
    ns = _real_namespace(repo, files)
    with pytest.raises(ValueError, match="at most once per call"):
        ns["dedupe_entity_files_parallel"](["E1", "E1"])
    assert files.deletes == []  # the guard fired BEFORE any task was built


def test_real_dedupe_backs_up_bytes_and_records_manifest_entries() -> None:
    """The revertability contract: every delete's bytes are persisted BEFORE
    the delete call (keyed (shared_id, file_id)) and each SUCCESSFUL delete is
    recorded on the manifest with the dedupe source — while soft-refused
    deletes back up bytes but record nothing (the file never went away)."""
    intercept = _PassthroughIntercept()
    repo, files = _incident_repos()
    throttle = ThrottleController()
    ns = _real_namespace(repo, files, throttle, intercept)
    summaries = ns["dedupe_entity_files_parallel"](["E1", "E2"])
    assert [s["deleted"] for s in summaries] == [2, 0]
    assert files.deletes == ["d2", "h2"]
    assert intercept.backups == {("E1", "d2"): b"SPANISH", ("E1", "h2"): b"HTML"}
    assert [r.file_id for r in intercept.deleted_file_records] == ["d2", "h2"]
    assert all(r.source == "dedupe" and r.shared_id == "E1" for r in intercept.deleted_file_records)


def test_real_dedupe_soft_failed_deletes_record_nothing_but_back_up() -> None:
    """A server-refused delete leaves the file in place: the bytes are backed
    up (the attempt was honest) but NO manifest record is written (revert
    must not re-upload a copy that never went away)."""
    intercept = _PassthroughIntercept()
    repo, refusing = _incident_repos(fail_deletes=True)
    ns = _real_namespace(repo, refusing, ThrottleController(), intercept)
    summaries = ns["dedupe_entity_files_parallel"](["E1"])
    assert summaries == [{"shared_id": "E1", "duplicates": 3, "deleted": 0, "failed": 2, "kept_cited": 1}]
    assert intercept.backups == {("E1", "d2"): b"SPANISH", ("E1", "h2"): b"HTML"}
    assert intercept.deleted_file_records == []  # nothing applied - nothing recorded


# --- empty input short-circuits in every mode ---------------------------------


def test_empty_input_returns_empty_list_in_every_mode() -> None:
    repo, files = _incident_repos()
    throttle = ThrottleController()
    real_ns = _real_namespace(repo, files, throttle)
    assert real_ns["dedupe_entity_files_parallel"]([]) == []
    assert files.deletes == []
    assert throttle.snapshot().success_streak == 0  # empty input records no verdict
    assert _dummy_namespace()["dedupe_entity_files_parallel"]([]) == []
    assert _dry_run_namespace([], repo, files)["dedupe_entity_files_parallel"]([]) == []
    assert files.deletes == []  # the dry run never deletes either
