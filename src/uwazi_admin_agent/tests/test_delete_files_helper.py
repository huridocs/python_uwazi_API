"""Isolated unit tests for the ``delete_entity_files_parallel`` bound helper.

Per AGENTS.md: no mocks/stubs, no network, no running Uwazi instance. The
namespaces are the REAL factories; the real helper is driven against tiny
REAL in-memory port classes (the ``test_dedupe_files_helper.py`` precedent)
plus an in-file recording intercept stand-in whose shared event log pins the
core's ordering. What is pinned:

- all three namespaces bind the same name with the same contract, so the SAME
  generated deletion script runs in validation, dry-run, and execute;
- the core's no-loss ordering: every delete's bytes are persisted BEFORE the
  delete call, and only SUCCESSFUL deletes are recorded on the manifest
  (a soft-``False`` delete left the file in place);
- the refusal rules end-to-end: ambiguous names list candidates, cited files
  are refused + reported, unbackable bytes are refused;
- soft ``False`` deletes count as ``failed`` + DEGRADED (never raise); a hard
  port exception kills the script loudly — AFTER the batch's applied deletes
  were recorded, so a partial batch stays revertable;
- the dry run RECORDS one ``delete_file`` op per would-be delete and one
  ``refuse_file`` op per refusal (the operator's review copy), deleting
  nothing.
"""

import asyncio
from typing import Any, override

import pytest

from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort
from uwazi_admin_agent.ports.file_repository_port import FileRepositoryPort
from uwazi_admin_agent.use_cases.script_exec_namespace import (
    ScopeViolationError,
    build_dry_run_namespace,
    build_exec_namespace,
    build_real_exec_namespace,
    run_script_sync,
)


class _RecordingIntercept:
    """Real stand-in for the BackupIntercept's file-deletion seams.

    ``events`` is the shared backup/delete trace (the file repo appends
    ``("delete", "", file_id)`` entries to the SAME list) so tests can assert
    the backup-BEFORE-delete ordering; ``backups`` persists the bytes keyed
    ``(shared_id, file_id)``; ``deleted_file_records`` collects the manifest
    records the helper writes on the script thread after the batch joins.
    """

    def __init__(self) -> None:
        self.backups: dict[tuple[str, str], bytes] = {}
        self.events: list[tuple[str, str, str]] = []
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

    def _save_file_backup(self, shared_id: str, file_id: str, data: bytes) -> None:
        self.backups[(shared_id, file_id)] = data
        self.events.append(("backup", shared_id, file_id))

    def _record_deleted_files(self, records: list) -> None:
        self.deleted_file_records.extend(records)


class _ExplodingCrud:
    """Raise if the script reaches CRUD (the deletion path never should)."""

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("CRUD helpers are not wired in these tests")


def _passthrough_crud() -> tuple:
    boom = _ExplodingCrud()
    return (boom, boom, boom, boom, boom, boom, boom)


class _EntityRepo(EntityRepositoryPort):
    """Real in-memory raw store; optionally raises for one shared_id."""

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


class _FileRepo(FileRepositoryPort):
    """Real in-memory byte store + delete recorder sharing the event log."""

    def __init__(
        self,
        bytes_store: dict[str, bytes],
        events: list[tuple[str, str, str]],
        *,
        fail_deletes: bool = False,
    ) -> None:
        self._bytes = bytes_store
        self._events = events
        self._fail = fail_deletes
        self.deletes: list[str] = []

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
        self._events.append(("delete", "", file_id))
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


def _incident() -> tuple[_EntityRepo, dict[str, bytes]]:
    """The incident's shape: FOUR same-named documents (1 English + 3 byte-identical
    Spanish copies), one unique attachment, one connection-cited copy."""
    raws = {
        "E1": _raw(
            "E1",
            documents=[
                {"_id": "d1", "originalname": "a.pdf", "filename": "f1", "size": 7},
                {"_id": "d2", "originalname": "a.pdf", "filename": "f2", "size": 7},
                {"_id": "d3", "originalname": "a.pdf", "filename": "f3", "size": 7},
                {"_id": "d4", "originalname": "a.pdf", "filename": "f4", "size": 7},
            ],
            attachments=[
                {"_id": "h1", "originalname": "doc.html", "filename": "g1", "size": 4},
                {"_id": "h2", "originalname": "extra.html", "filename": "g2", "size": 5},
            ],
            relations=[{"entity": "E1", "file": "d3"}],  # a text reference cites the d3 copy
        )
    }
    bytes_store = {"f1": b"SPANISH", "f2": b"SPANISH", "f3": b"SPANISH", "f4": b"ENGLISH", "g1": b"HTML", "g2": b"HTML2"}
    return _EntityRepo(raws), bytes_store


def _wired(
    intercept: _RecordingIntercept, fail_deletes: bool = False, explode_on: str | None = None
) -> tuple[dict, _FileRepo]:
    repo, bytes_store = _incident()
    repo = _EntityRepo(dict(repo._raws), explode_on=explode_on) if explode_on else repo
    files = _FileRepo(dict(bytes_store), intercept.events, fail_deletes=fail_deletes)
    ns = build_real_exec_namespace(
        entity_api=None,  # type: ignore[arg-type]
        relationship_api=None,
        loop=asyncio.new_event_loop(),
        intercept=intercept,
        tool_cache=None,
        default_language="en",
        entity_repository=repo,
        file_repository=files,
    )
    return ns, files


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
    entity_repository: _EntityRepo | None = None,
    file_repository: _FileRepo | None = None,
) -> dict:
    return build_dry_run_namespace(
        entity_api=None,
        loop=asyncio.new_event_loop(),
        file_repository=file_repository,
        default_language="en",
        dry_run_records=records,
        entity_repository=entity_repository,
    )


# --- all three namespaces bind the same name --------------------------------


def test_all_three_namespaces_bind_the_delete_name() -> None:
    assert "delete_entity_files_parallel" in _dummy_namespace()
    assert "delete_entity_files_parallel" in _dry_run_namespace([])
    assert "delete_entity_files_parallel" in build_real_exec_namespace(
        entity_api=None,  # type: ignore[arg-type]
        relationship_api=None,
        loop=asyncio.new_event_loop(),
        intercept=_RecordingIntercept(),
        tool_cache=None,
        default_language="en",
    )


# --- dummy mode: scoped no-op ------------------------------------------------


def test_dummy_delete_noops_per_distinct_entity_in_first_appearance_order() -> None:
    ns = _dummy_namespace()
    summaries = ns["delete_entity_files_parallel"](
        [
            {"shared_id": "E1", "file_id": "d1"},
            {"shared_id": "E2", "file_id": "x1"},
            {"shared_id": "E1", "file_id": "d2"},
        ]
    )
    assert summaries == [
        {"shared_id": "E1", "requested": 2, "deleted": 0, "failed": 0, "refused": 0, "refusals": []},
        {"shared_id": "E2", "requested": 1, "deleted": 0, "failed": 0, "refused": 0, "refusals": []},
    ]


def test_dummy_delete_refuses_out_of_scope_ids() -> None:
    ns = _dummy_namespace()
    with pytest.raises(ScopeViolationError):
        ns["delete_entity_files_parallel"]([{"shared_id": "REAL", "file_id": "d1"}])


def test_dummy_delete_refuses_a_duplicated_request() -> None:
    ns = _dummy_namespace()
    with pytest.raises(ValueError, match="at most once per call"):
        ns["delete_entity_files_parallel"]([{"shared_id": "E1", "file_id": "d1"}, {"shared_id": "E1", "file_id": "d1"}])


# --- dry-run mode: real discovery reads, recorded deletes ---------------------


def test_dry_run_records_would_be_deletes_and_refusals() -> None:
    records: list = []
    repo, bytes_store = _incident()
    events: list = []
    files = _FileRepo(bytes_store, events)
    ns = _dry_run_namespace(records, repo, files)
    summaries = ns["delete_entity_files_parallel"](
        [
            {"shared_id": "E1", "file_id": "d2"},  # byte-identical Spanish copy: deletable
            {"shared_id": "E1", "file_id": "d4"},  # the English original: deletable (explicit!)
            {"shared_id": "E1", "file_id": "d3"},  # connection-cited: refused
            {"shared_id": "E1", "originalname": "a.pdf"},  # FOUR matches: ambiguous
            {"shared_id": "E1", "file_id": "dX"},  # not on the entity: not_found
        ]
    )
    assert summaries == [
        {
            "shared_id": "E1",
            "requested": 5,
            "deleted": 2,
            "failed": 0,
            "refused": 3,
            "refusals": [
                {
                    "shared_id": "E1",
                    "file_id": "d3",
                    "originalname": "a.pdf",
                    "kind": "document",
                    "reason": "cited",
                    "matches": [],
                },
                {
                    "shared_id": "E1",
                    "file_id": None,
                    "originalname": "a.pdf",
                    "kind": None,
                    "reason": "ambiguous",
                    "matches": ["d1", "d2", "d3", "d4"],
                },
                {
                    "shared_id": "E1",
                    "file_id": "dX",
                    "originalname": None,
                    "kind": None,
                    "reason": "not_found",
                    "matches": [],
                },
            ],
        }
    ]
    assert records == [
        {"op": "delete_file", "shared_id": "E1", "file_id": "d2", "originalname": "a.pdf", "filename": "f2"},
        {"op": "delete_file", "shared_id": "E1", "file_id": "d4", "originalname": "a.pdf", "filename": "f4"},
        {
            "op": "refuse_file",
            "shared_id": "E1",
            "file_id": "d3",
            "originalname": "a.pdf",
            "kind": "document",
            "reason": "cited",
            "matches": [],
        },
        {
            "op": "refuse_file",
            "shared_id": "E1",
            "file_id": None,
            "originalname": "a.pdf",
            "kind": None,
            "reason": "ambiguous",
            "matches": ["d1", "d2", "d3", "d4"],
        },
        {
            "op": "refuse_file",
            "shared_id": "E1",
            "file_id": "dX",
            "originalname": None,
            "kind": None,
            "reason": "not_found",
            "matches": [],
        },
    ]
    assert files.deletes == []  # the dry run never deletes


def test_dry_run_records_unavailable_for_unfetchable_bytes() -> None:
    records: list = []
    repo, bytes_store = _incident()
    del bytes_store["f2"]  # the d2 copy's bytes cannot be fetched
    events: list = []
    files = _FileRepo(bytes_store, events)
    ns = _dry_run_namespace(records, repo, files)
    summaries = ns["delete_entity_files_parallel"]([{"shared_id": "E1", "file_id": "d2"}])
    assert summaries[0]["deleted"] == 0
    assert summaries[0]["refusals"] == [
        {
            "shared_id": "E1",
            "file_id": "d2",
            "originalname": "a.pdf",
            "kind": "document",
            "reason": "unavailable",
            "matches": [],
        }
    ]
    assert records[0]["op"] == "refuse_file"


def test_dry_run_refuses_duplicated_request_without_recording() -> None:
    records: list = []
    repo, bytes_store = _incident()
    events: list = []
    ns = _dry_run_namespace(records, repo, _FileRepo(bytes_store, events))
    with pytest.raises(ValueError, match="at most once per call"):
        ns["delete_entity_files_parallel"]([{"shared_id": "E1", "file_id": "d2"}, {"shared_id": "E1", "file_id": "d2"}])
    assert records == []


def test_dry_run_unwired_ports_raise_runtime_error() -> None:
    ns = _dry_run_namespace([])
    with pytest.raises(RuntimeError, match="delete_entity_files_parallel requires a wired"):
        ns["delete_entity_files_parallel"]([{"shared_id": "E1", "file_id": "d2"}])


def test_dry_run_empty_returns_empty() -> None:
    records: list = []
    repo, bytes_store = _incident()
    events: list = []
    files = _FileRepo(bytes_store, events)
    ns = _dry_run_namespace(records, repo, files)
    assert ns["delete_entity_files_parallel"]([]) == []
    assert records == []
    assert files.deletes == []


# --- real mode: the deletion contract end-to-end -----------------------------


def test_real_delete_backs_up_before_deleting_and_records_manifest() -> None:
    """THE ordering invariant: per file, bytes persisted BEFORE the delete
    call (never after), then only successful deletes recorded with the
    explicit source for revert."""
    intercept = _RecordingIntercept()
    ns, files = _wired(intercept)
    summaries = ns["delete_entity_files_parallel"](
        [
            {"shared_id": "E1", "file_id": "d2"},  # Spanish duplicate
            {"shared_id": "E1", "file_id": "d4"},  # the English original (explicit intent deletes it)
            {"shared_id": "E1", "file_id": "h2"},  # the extra attachment
        ]
    )
    assert summaries == [{"shared_id": "E1", "requested": 3, "deleted": 3, "failed": 0, "refused": 0, "refusals": []}]
    assert files.deletes == ["d2", "d4", "h2"]
    # backup-then-delete, per file, in order — never delete-then-backup.
    assert intercept.events == [
        ("backup", "E1", "d2"),
        ("delete", "", "d2"),
        ("backup", "E1", "d4"),
        ("delete", "", "d4"),
        ("backup", "E1", "h2"),
        ("delete", "", "h2"),
    ]
    assert intercept.backups == {("E1", "d2"): b"SPANISH", ("E1", "d4"): b"ENGLISH", ("E1", "h2"): b"HTML2"}
    assert [r.file_id for r in intercept.deleted_file_records] == ["d2", "d4", "h2"]
    assert all(r.source == "explicit" and r.shared_id == "E1" for r in intercept.deleted_file_records)


def test_real_delete_refuses_cited_ambiguous_and_unbackable_targets() -> None:
    intercept = _RecordingIntercept()
    ns, files = _wired(intercept)
    summaries = ns["delete_entity_files_parallel"](
        [
            {"shared_id": "E1", "file_id": "d3"},  # cited -> refused, NOT deleted
            {"shared_id": "E1", "originalname": "a.pdf"},  # ambiguous -> refused with candidates
            {"shared_id": "E1", "file_id": "d2", "originalname": "zz.pdf"},  # file_id wins: deleted
        ]
    )
    assert summaries[0]["deleted"] == 1
    assert summaries[0]["refused"] == 2
    reasons = {r["file_id"]: r["reason"] for r in summaries[0]["refusals"]}
    assert reasons == {"d3": "cited", None: "ambiguous"}
    assert files.deletes == ["d2"]  # only the unambiguous, uncited, fetchable target
    assert [r.file_id for r in intercept.deleted_file_records] == ["d2"]


def test_real_delete_soft_failures_count_failed_and_never_raise() -> None:
    """The server refuses every delete (soft bool-False): each attempt counts
    as ``failed``, bytes ARE backed up (the attempt was honest), but NO
    manifest record is written (the files never went away)."""
    intercept = _RecordingIntercept()
    ns, files = _wired(intercept, fail_deletes=True)
    summaries = ns["delete_entity_files_parallel"]([{"shared_id": "E1", "file_id": "d2"}])
    assert summaries == [{"shared_id": "E1", "requested": 1, "deleted": 0, "failed": 1, "refused": 0, "refusals": []}]
    assert intercept.backups == {("E1", "d2"): b"SPANISH"}
    assert intercept.deleted_file_records == []


def test_real_task_failure_surfaces_as_script_error_after_recording() -> None:
    """A hard port EXCEPTION inside one task kills the script loudly — but
    only AFTER the OTHER entities' successful deletes were recorded (the
    partial batch stays revertable)."""
    intercept = _RecordingIntercept()
    raws = {
        "E1": _raw("E1", documents=[{"_id": "d1", "originalname": "a.pdf", "filename": "f1", "size": 7}]),
        "E2": _raw("E2"),
    }
    repo = _EntityRepo(raws, explode_on="E2")
    files = _FileRepo({"f1": b"A"}, intercept.events)
    ns = build_real_exec_namespace(
        entity_api=None,  # type: ignore[arg-type]
        relationship_api=None,
        loop=asyncio.new_event_loop(),
        intercept=intercept,
        tool_cache=None,
        default_language="en",
        entity_repository=repo,
        file_repository=files,
    )
    code = """
result = delete_entity_files_parallel([
    {"shared_id": "E1", "file_id": "d1"},
    {"shared_id": "E2", "file_id": "x1"},
])
"""
    result, error = run_script_sync(code, ns)
    assert result is None
    assert error is not None
    assert "RuntimeError: fetch failed for E2" in error
    # E1's applied delete was recorded BEFORE the re-raise: revertable.
    assert [r.file_id for r in intercept.deleted_file_records] == ["d1"]
    assert files.deletes == ["d1"]


def test_real_delete_refuses_duplicated_request_before_any_io() -> None:
    intercept = _RecordingIntercept()
    ns, files = _wired(intercept)
    with pytest.raises(ValueError, match="at most once per call"):
        ns["delete_entity_files_parallel"]([{"shared_id": "E1", "file_id": "d2"}, {"shared_id": "E1", "file_id": "d2"}])
    assert files.deletes == []
    assert intercept.backups == {}


def test_request_without_shared_id_fails_identically_in_every_mode() -> None:
    """A malformed request is never silently dropped (that would hide the loss
    of a deletion): every mode raises the same ValueError up front."""
    malformed = [{"file_id": "d1"}]
    with pytest.raises(ValueError, match="must name a shared_id"):
        _dummy_namespace()["delete_entity_files_parallel"](malformed)
    records: list = []
    repo, bytes_store = _incident()
    events: list = []
    with pytest.raises(ValueError, match="must name a shared_id"):
        _dry_run_namespace(records, repo, _FileRepo(bytes_store, events))["delete_entity_files_parallel"](malformed)
    intercept = _RecordingIntercept()
    real_ns, files = _wired(intercept)
    with pytest.raises(ValueError, match="must name a shared_id"):
        real_ns["delete_entity_files_parallel"](malformed)
    assert records == []
    assert files.deletes == []
    assert intercept.backups == {}


def test_real_unwired_ports_raise_runtime_error_as_a_script_error() -> None:
    ns = build_real_exec_namespace(
        entity_api=None,  # type: ignore[arg-type]
        relationship_api=None,
        loop=asyncio.new_event_loop(),
        intercept=_RecordingIntercept(),
        tool_cache=None,
        default_language="en",
    )
    code = 'delete_entity_files_parallel([{"shared_id": "E1", "file_id": "d1"}])'
    result, error = run_script_sync(code, ns)
    assert result is None
    assert error is not None
    assert "delete_entity_files_parallel requires a wired" in error


def test_empty_input_returns_empty_list_in_every_mode() -> None:
    intercept = _RecordingIntercept()
    real_ns, files = _wired(intercept)
    assert real_ns["delete_entity_files_parallel"]([]) == []
    assert files.deletes == []
    assert intercept.deleted_file_records == []
    assert _dummy_namespace()["delete_entity_files_parallel"]([]) == []
    records: list = []
    repo, bytes_store = _incident()
    events: list = []
    assert _dry_run_namespace(records, repo, _FileRepo(bytes_store, events))["delete_entity_files_parallel"]([]) == []
    assert records == []
