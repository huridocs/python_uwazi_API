"""Isolated unit tests for the dry-run namespace + use case (per AGENTS.md).

Pure tests only: literal inputs, real ``run_script_sync`` + namespace
composition, no mocks, no network, no running Uwazi instance. The unwired
ports (``None``) exercise the real factories' loud-failure stubs; the
end-to-end script test feeds HTML through a bound literal so no I/O port is
ever touched.
"""

import asyncio

import pytest

from uwazi_admin_agent.use_cases.dry_run_script_use_case import (
    DryRunScriptUseCase,
    _count_ops,
)
from uwazi_admin_agent.use_cases.script_exec_namespace import (
    _dry_run_write_helpers,
    build_dry_run_namespace,
    run_script_sync,
)

# --- _dry_run_write_helpers --------------------------------------------------


def test_dry_run_write_helpers_record_without_io() -> None:
    records: list[dict] = []
    helpers = _dry_run_write_helpers(records)

    created = helpers["create_entities"]([{"title": "T1", "template_name": "tmpl", "metadata": {"a": 1}}], language="en")
    updated = helpers["update_entities"]([{"shared_id": "abc", "metadata": {"k": "v"}}])
    deleted = helpers["delete_entities"](["x1", "x2"])
    published = helpers["publish_entities"](["p1"])
    unpublished = helpers["unpublish_entities"](["p2"])
    status = helpers["set_publish_status"](["p3"], False)
    rels = helpers["create_relationships"]([{"from": "a", "to": "b"}])

    # Return shapes match the real helpers' documented shapes.
    assert created == [{"shared_id": "dry-created-0", "success": True}]
    assert updated == [{"shared_id": "abc", "success": True}]
    assert deleted == [{"shared_id": "x1", "success": True}, {"shared_id": "x2", "success": True}]
    assert published == {
        "success_count": 1,
        "failure_count": 0,
        "rate_limited": [],
        "permission_denied": [],
        "not_found": [],
        "errors": [],
    }
    assert unpublished == published
    assert status == [{"shared_id": "p3", "success": True}]
    assert rels == [{"success": True}]

    # The records list holds the exact per-op records.
    assert records == [
        {"op": "create", "title": "T1", "template_name": "tmpl", "metadata": {"a": 1}},
        {
            "op": "update",
            "shared_id": "abc",
            "template_name": None,
            "metadata": {"k": "v"},
            "title": None,
            "language": None,
        },
        {"op": "delete", "shared_id": "x1"},
        {"op": "delete", "shared_id": "x2"},
        {"op": "publish", "shared_id": "p1"},
        {"op": "unpublish", "shared_id": "p2"},
        {"op": "set_publish_status", "shared_id": "p3", "published": False},
        {"op": "create_relationships", "from": "a", "to": "b"},
    ]
    assert helpers["_dry_run_records"] is records


def test_dry_run_created_ids_never_collide_with_real_shared_ids() -> None:
    records: list[dict] = []
    helpers = _dry_run_write_helpers(records)
    results = helpers["create_entities"]([{"title": "x"}, {"title": "y"}])
    assert [r["shared_id"] for r in results] == ["dry-created-0", "dry-created-1"]
    assert all(r["shared_id"].startswith("dry-created-") for r in results)


# --- build_dry_run_namespace -------------------------------------------------


def test_dry_run_namespace_binds_real_readers_and_dry_writers() -> None:
    ns = build_dry_run_namespace(
        entity_api=None,
        loop=asyncio.new_event_loop(),
        file_repository=None,
        default_language="en",
        dry_run_records=[],
        entity_repository=None,
    )
    # The unwired-real stubs raise RuntimeError — proves they are the REAL
    # factories' stubs, not fresh fakes.
    with pytest.raises(RuntimeError):
        ns["query_entities"]("by_ids")
    with pytest.raises(RuntimeError):
        ns["get_entity_files"]("some-id")
    with pytest.raises(RuntimeError):
        ns["get_file_bytes"]("file.pdf")
    # The seven write helpers are recording stubs (no I/O, success shapes).
    records: list[dict] = ns["_dry_run_records"]
    assert ns["update_entities"]([{"shared_id": "s1"}]) == [{"shared_id": "s1", "success": True}]
    assert records == [
        {"op": "update", "shared_id": "s1", "template_name": None, "metadata": None, "title": None, "language": None}
    ]
    # move_files_to_entity is a dry recorder too.
    assert ns["move_files_to_entity"](["a", "b"], "c") == {"moved": 2, "failed": 0}
    assert ns["_dry_run_records"][-1] == {"op": "move_files", "from_shared_ids": ["a", "b"], "to_shared_id": "c"}
    # Same stdlib + SAFE_BUILTINS contract as the other namespaces.
    from uwazi_admin_agent.use_cases.script_exec_namespace import _STDLIB, SAFE_BUILTINS

    assert ns["json"] is _STDLIB["json"]
    assert ns["__builtins__"] is SAFE_BUILTINS


# --- end-to-end extraction script through run_script_sync --------------------


_TABLE_HTML = (
    "<html><body><table>"
    "<tr><td>2006-10-02</td><td>Press release</td></tr>"
    "<tr><td>2007-03-15</td><td>Annual report</td></tr>"
    "</table></body></html>"
)

# The ctx contract the system prompt declares: extract(html, ctx) where ctx
# carries per-entity supporting-file data. The script reads the literal table,
# builds update dicts WITHOUT title, and calls update_entities in one chunk.
_SCRIPT = """
rows = htmlextract.tables(HTML)[0]

def extract(html, ctx):
    cells = htmlextract.tables(ctx["html"])[0][0]
    return {"date_published": cells[0], "description": cells[1]}

updates = []
for row in rows:
    # Each row carries its own supporting-file HTML through the ctx contract.
    ctx = {"row": row, "html": "<table><tr><td>%s</td><td>%s</td></tr></table>" % (row[0], row[1])}
    extracted = extract(ctx["html"], ctx)
    updates.append({
        "shared_id": ctx["row"][1].lower().replace(" ", "-"),
        "metadata": extracted,
    })
result = update_entities(updates)
"""


def test_dry_run_end_to_end_extraction_script() -> None:
    records: list[dict] = []
    ns = build_dry_run_namespace(
        entity_api=None,
        loop=asyncio.new_event_loop(),
        file_repository=None,
        default_language="en",
        dry_run_records=records,
        entity_repository=None,
    )
    # The script's HTML arrives via a bound literal — no file I/O port involved.
    ns["HTML"] = _TABLE_HTML
    result, error = run_script_sync(_SCRIPT, ns)

    assert error is None
    assert result == str([{"shared_id": "press-release", "success": True}, {"shared_id": "annual-report", "success": True}])
    assert records == [
        {
            "op": "update",
            "shared_id": "press-release",
            "template_name": None,
            "metadata": {"date_published": "2006-10-02", "description": "Press release"},
            "title": None,
            "language": None,
        },
        {
            "op": "update",
            "shared_id": "annual-report",
            "template_name": None,
            "metadata": {"date_published": "2007-03-15", "description": "Annual report"},
            "title": None,
            "language": None,
        },
    ]


# --- use case aggregation ----------------------------------------------------


def test_dry_run_use_case_aggregates_counters() -> None:
    use_case = DryRunScriptUseCase(entity_api=None, entity_repository=None, file_repository=None, default_language="en")
    # The script reads its HTML from a bound literal (the sandbox's HTML
    # variable); the use case accepts a prelude for exactly this offline path.
    report = use_case._dry_run_sync(f"HTML = {_TABLE_HTML!r}\n{_SCRIPT}")

    assert report.passed is True
    assert report.script_error is None
    assert report.would_update == 2
    assert report.would_create == 0
    assert report.would_delete == 0
    assert report.would_publish == 0
    assert report.would_unpublish == 0
    assert report.would_rewire == 0
    assert len(report.records) == 2
    assert report.records[0]["metadata"]["date_published"] == "2006-10-02"


def test_dry_run_use_case_reports_script_error_as_failed() -> None:
    use_case = DryRunScriptUseCase(entity_api=None, entity_repository=None, file_repository=None, default_language="en")
    report = use_case._dry_run_sync("result = 1 / 0")

    assert report.passed is False
    assert report.script_error is not None
    assert "ZeroDivisionError" in report.script_error
    assert report.would_update == 0


def test_count_ops_splits_publish_kinds() -> None:
    records = [
        {"op": "update"},
        {"op": "publish", "shared_id": "a"},
        {"op": "unpublish", "shared_id": "b"},
        {"op": "set_publish_status", "shared_id": "c", "published": True},
        {"op": "set_publish_status", "shared_id": "d", "published": False},
        {"op": "move_files", "from_shared_ids": ["e"], "to_shared_id": "f"},
        {"op": "create_relationships"},
    ]
    counts = _count_ops(records)
    assert counts["update"] == 1
    assert counts["publish"] == 2  # publish + set_publish_status(published=True)
    assert counts["publish:False"] == 2  # unpublish + set_publish_status(published=False)
    assert counts["move_files"] == 1
    assert counts["create_relationships"] == 1
