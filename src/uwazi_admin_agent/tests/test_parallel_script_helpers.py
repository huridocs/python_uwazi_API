"""Isolated unit tests for the ``*_parallel`` bound helpers across the 3 namespaces.

Per AGENTS.md: no mocks/stubs, no network, no running Uwazi instance. The
namespaces are the REAL factories built with unwired ports (``None``) — the
existing convention (``test_script_exec_namespace.py``) — plus a minimal
in-file intercept stand-in for the real namespace's write path (the same
``_Decorate`` precedent that file established). What is pinned:

- every namespace binds the same parallel names (the 5 here, plus the
  parallel file-move name pinned in ``test_parallel_move_files_helper.py``),
  so the SAME generated script runs in dummy / dry-run / real mode;
- dummy + dry-run write names ALIAS their sequential twins (identical
  behavior by construction);
- unwired parallel read helpers fail LOUDLY (RuntimeError), like the
  sequential unwired stubs;
- a write task failure is re-raised as a script error after being reported
  to the throttle (the on-error policy path stays intact).

The live parallel flow (real ports, real HTTP) is validated via the
simulation run, like the rest of the intercept path.
"""

import asyncio

from uwazi_admin_agent.use_cases.script_exec_namespace import (
    build_dry_run_namespace,
    build_exec_namespace,
    build_real_exec_namespace,
    run_script_sync,
)

_WRITE_NAMES = ("update_entities_parallel", "create_entities_parallel", "create_relationships_parallel")
_READ_NAMES = ("get_entity_files_parallel", "get_file_bytes_parallel")


class _PassthroughIntercept:
    """Minimal real stand-in for the BackupIntercept seam (no ports involved)."""

    def decorate(self, crud):
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

    def _backup_before_modify(self, shared_ids, language):
        pass

    def _backup_before_rewire(self, shared_ids, language):
        pass

    def _record_created(self, results):
        pass

    def _invalidate(self, shared_ids):
        pass

    def _emit(self, op_kind, shared_ids, **kwargs):
        pass


def _dummy_namespace() -> dict:
    return build_exec_namespace(
        entity_api=None,
        relationship_api=None,
        loop=asyncio.new_event_loop(),
        scope={"DUMMY1"},
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


def _real_namespace() -> dict:
    return build_real_exec_namespace(
        entity_api=None,
        relationship_api=None,
        loop=asyncio.new_event_loop(),
        intercept=_PassthroughIntercept(),
        tool_cache=None,
        default_language="en",
        entity_repository=None,
        file_repository=None,
    )


# --- all three namespaces bind the same 5 names -------------------------------


def test_dummy_namespace_binds_the_parallel_names() -> None:
    ns = _dummy_namespace()
    assert all(name in ns for name in _WRITE_NAMES + _READ_NAMES)


def test_dry_run_namespace_binds_the_parallel_names() -> None:
    ns = _dry_run_namespace([])
    assert all(name in ns for name in _WRITE_NAMES + _READ_NAMES)


def test_real_namespace_binds_the_parallel_names() -> None:
    ns = _real_namespace()
    assert all(name in ns for name in _WRITE_NAMES + _READ_NAMES)


# --- dummy mode: aliases + scoped no-op reads ----------------------------------


def test_dummy_parallel_writes_alias_the_scoped_sequential_helpers() -> None:
    """Aliasing IS the dummy contract: same scope enforcement, same shapes, zero divergence."""
    ns = _dummy_namespace()
    assert ns["update_entities_parallel"] is ns["update_entities"]
    assert ns["create_entities_parallel"] is ns["create_entities"]
    assert ns["create_relationships_parallel"] is ns["create_relationships"]


def test_dummy_parallel_reads_mirror_the_single_item_noops() -> None:
    ns = _dummy_namespace()
    code = """
bytes_map = get_file_bytes_parallel(["f1", "f2"])
in_files = get_entity_files_parallel(["DUMMY1"])
refused = "none"
try:
    get_entity_files_parallel(["REAL_ID"])
except Exception as exc:
    refused = "refused" if "refused shared_id(s) outside the dummy scope" in str(exc) else "other"
result = f"{len(bytes_map)}|{bytes_map['f1']}|{len(in_files['DUMMY1'])}|{refused}"
"""
    result, error = run_script_sync(code, ns)
    assert error is None
    assert result == "2|None|0|refused"


# --- dry-run mode: recorder write aliases + unwired read stubs ----------------


def test_dry_run_parallel_writes_record_exactly_like_the_sequential_calls() -> None:
    records: list = []
    ns = _dry_run_namespace(records)
    assert ns["update_entities_parallel"] is ns["update_entities"]
    code = """
updates = [{"shared_id": "A", "template_name": "T", "metadata": {"p": "v"}}]
u = update_entities_parallel(updates)
c = create_entities_parallel([{"title": "x", "template_name": "T"}])
rel = {"from_entity_shared_id": "A", "to_entity_shared_id": "B", "relationship_type_name": "rel"}
r = create_relationships_parallel([rel])
result = f"{len(u)}|{u[0]['success']}|{len(c)}|{len(r)}"
"""
    result, error = run_script_sync(code, ns)
    assert error is None
    assert result == "1|True|1|1"
    assert [rec["op"] for rec in records] == ["update", "create", "create_relationships"]


def test_dry_run_parallel_reads_fail_loudly_when_unwired() -> None:
    ns = _dry_run_namespace([])
    code = """
e1 = e2 = "no-raise"
try:
    get_entity_files_parallel(["A"])
except RuntimeError:
    e1 = "RuntimeError"
try:
    get_file_bytes_parallel(["f"])
except RuntimeError:
    e2 = "RuntimeError"
result = e1 + "|" + e2
"""
    result, error = run_script_sync(code, ns)
    assert error is None
    assert result == "RuntimeError|RuntimeError"


# --- real mode: unwired read stubs + write error semantics ---------------------


def test_real_parallel_reads_fail_loudly_when_unwired() -> None:
    ns = _real_namespace()
    code = """
e1 = e2 = "no-raise"
try:
    get_entity_files_parallel(["A"])
except RuntimeError:
    e1 = "RuntimeError"
try:
    get_file_bytes_parallel(["f"])
except RuntimeError:
    e2 = "RuntimeError"
result = e1 + "|" + e2
"""
    result, error = run_script_sync(code, ns)
    assert error is None
    assert result == "RuntimeError|RuntimeError"


def test_real_write_task_failure_raises_as_a_script_error() -> None:
    """A chunk that fails is reported to the throttle, then re-raised — the
    script error surface (and the on-error policy behind it) stays intact."""
    ns = _real_namespace()
    code = """
outcome = "no-raise"
try:
    update_entities_parallel([{"shared_id": "A", "template_name": "T"}])
except AttributeError:
    outcome = "AttributeError"
result = outcome
"""
    result, error = run_script_sync(code, ns)
    assert error is None
    assert result == "AttributeError"


def test_real_write_helpers_short_circuit_on_empty_input() -> None:
    ns = _real_namespace()
    code = """
u = update_entities_parallel([])
c = create_entities_parallel([])
result = f"{u}|{c}"
"""
    result, error = run_script_sync(code, ns)
    assert error is None
    assert result == "[]|[]"


def test_real_relationships_parallel_without_api_mirrors_the_sequential_error() -> None:
    ns = _real_namespace()
    code = """
rel = {"from_entity_shared_id": "A", "to_entity_shared_id": "B", "relationship_type_name": "r"}
r = create_relationships_parallel([rel])
result = f"{len(r)}|{r[0]['success']}|{r[0]['error']}"
"""
    result, error = run_script_sync(code, ns)
    assert error is None
    assert result == "1|False|Relationship API not configured"
