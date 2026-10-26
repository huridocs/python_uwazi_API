"""Isolated unit tests for merge detection and the two merge ops (per AGENTS.md).

Pure transforms plus a real SQLite round-trip on ``tmp_path``: script records in,
reviewable suggestions / apply payloads out. No mocks, no network, no instance.

The contract under test: a scripted merge (a file move plus a delete for every
moved source, with or without a target write) is ONE review row — the merge op
on the TARGET, with the source ids riding along — and its apply payloads are the
ordinary update/create payloads plus one bulk delete of the sources.
"""

import sqlite3
from datetime import datetime, timezone

from uwazi_admin_agent.adapters.sqlite_suggestion_store import SqliteSuggestionStore
from uwazi_admin_agent.domain.suggestion import (
    TITLE_PROPERTY,
    EntityReviewRow,
    OperationReviewRow,
    Suggestion,
    SuggestionFilter,
    SuggestionStatus,
)
from uwazi_admin_agent.domain.suggestion_scoring import (
    drop_unchanged_property_rows,
    merge_operations,
    records_to_suggestions,
)
from uwazi_admin_agent.drivers.web import operations_view as view
from uwazi_admin_agent.drivers.web.review_view import entity_row, run_confirmation
from uwazi_admin_agent.use_cases.apply_suggestions_use_case import (
    apply_units_total,
    build_apply_creates,
    build_apply_updates,
    file_move_targets,
    merge_deletions,
    merge_moves,
    partition_for_apply,
    pushable_rows,
)

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _record(op: str, **kwargs: object) -> dict[str, object]:
    return {"op": op, **kwargs}


def _suggestion(
    *,
    shared_id: str = "s1",
    language: str | None = None,
    op: str = "update",
    property_name: str | None = "excerpt",
    proposed: object = "v",
    template_name: str | None = None,
    merge_source_ids: list[str] | None = None,
    status: SuggestionStatus = SuggestionStatus.ACCEPTED,
) -> Suggestion:
    return Suggestion(
        run_id="run1",
        shared_id=shared_id,
        language=language,
        op=op,  # type: ignore[arg-type]
        property_name=property_name,
        proposed_value=proposed,
        template_name=template_name,
        merge_source_ids=merge_source_ids or [],
        status=status,
        created_at=NOW,
    )


# --- merge_operations / records_to_suggestions --------------------------------


def test_update_merge_collapses_into_one_target_with_its_sources() -> None:
    records = [
        _record("update", shared_id="T", template_name="Case", title="Target", metadata={"a": 1}),
        _record("move_files", from_shared_ids=["S1", "S2"], to_shared_id="T"),
        _record("delete", shared_id="S1"),
        _record("delete", shared_id="S2"),
    ]

    rows = records_to_suggestions("run1", records)

    assert [(r.op, r.shared_id, r.property_name) for r in rows] == [
        ("update_and_delete", "T", TITLE_PROPERTY),
        ("update_and_delete", "T", "a"),
    ]
    assert all(r.merge_source_ids == ["S1", "S2"] for r in rows)
    # The source deletes are absorbed: no row addresses S1/S2 on its own.
    assert {r.shared_id for r in rows} == {"T"}


def test_create_merge_uses_the_create_and_delete_op_and_the_placeholder() -> None:
    records = [
        _record("create", title="New", template_name="Case", metadata={"a": 1}),
        _record("move_files", from_shared_ids=["S1"], to_shared_id="dry-created-0"),
        _record("delete", shared_id="S1"),
    ]

    rows = records_to_suggestions("run1", records)

    assert [(r.op, r.shared_id, r.property_name) for r in rows] == [
        ("create_and_delete", "dry-created-0", TITLE_PROPERTY),
        ("create_and_delete", "dry-created-0", "a"),
    ]
    assert rows[0].merge_source_ids == ["S1"]


def test_a_move_plus_source_delete_without_a_target_write_is_one_atomic_merge() -> None:
    """The dangerous split case: copy files, then remove the source.

    Without a target write the old plan became a copy row plus an independently
    runnable delete row; running the delete alone would throw the source's files
    away before their copy. The copy and the delete are therefore ONE merge row
    (a propertyless anchor: no target metadata is written).
    """
    files = [{"from_shared_id": "S1", "kind": "document", "originalname": "source.pdf"}]
    records = [
        _record("move_files", from_shared_ids=["S1"], to_shared_id="T", files=files),
        _record("delete", shared_id="S1"),
    ]

    rows = records_to_suggestions("run1", records)

    assert [(r.op, r.shared_id, r.property_name, r.proposed_value) for r in rows] == [
        ("update_and_delete", "T", None, None),
    ]
    assert rows[0].merge_source_ids == ["S1"]
    assert rows[0].merge_files == files
    # The source delete is absorbed, not a second row.
    assert {row.shared_id for row in rows} == {"T"}


def test_a_move_without_any_delete_is_still_a_plain_copy() -> None:
    records = [
        _record("move_files", from_shared_ids=["S1"], to_shared_id="T"),
    ]

    rows = records_to_suggestions("run1", records)

    assert [(r.op, r.shared_id) for r in rows] == [("move_files", "T")]


def test_an_update_plus_move_without_a_source_delete_is_not_a_merge() -> None:
    """Without the delete the operator keeps the source: a copy, never a merge.

    Treating it as a merge would delete a source the script never asked to
    remove. The target write and the file copy are two independent operations.
    """
    records = [
        _record("update", shared_id="T", metadata={"a": 1}),
        _record("move_files", from_shared_ids=["S1"], to_shared_id="T", files=[]),
    ]

    rows = records_to_suggestions("run1", records)

    assert [(r.op, r.shared_id) for r in rows] == [("update", "T"), ("move_files", "T")]
    assert rows[0].merge_source_ids == []
    assert rows[1].merge_source_ids == ["S1"]


def test_a_move_only_plan_is_one_runnable_copy_row() -> None:
    """The reported defect: a plan whose only work is a file copy had no row."""
    files = [
        {"from_shared_id": "S1", "kind": "document", "originalname": "source.pdf"},
        {"from_shared_id": "S1", "kind": "attachment", "originalname": "annex.html"},
    ]
    records = [
        _record("move_files", from_shared_ids=["S1"], to_shared_id="T", files=files),
    ]

    rows = records_to_suggestions("run1", records)

    assert len(rows) == 1
    row = rows[0]
    assert (row.op, row.shared_id, row.property_name, row.proposed_value) == (
        "move_files",
        "T",
        None,
        None,
    )
    assert row.merge_source_ids == ["S1"]
    assert row.merge_files == files
    assert row.status == SuggestionStatus.PENDING


def test_an_update_without_a_move_keeps_its_delete_apart() -> None:
    records = [
        _record("update", shared_id="T", metadata={"a": 1}),
        _record("delete", shared_id="S1"),
    ]

    rows = records_to_suggestions("run1", records)

    assert [(r.op, r.shared_id) for r in rows] == [("update", "T"), ("delete", "S1")]


def test_a_move_that_deletes_only_some_sources_is_not_a_merge() -> None:
    """Every moved source must be deleted for a merge; a kept source is a copy.

    Collapsing this into a merge would delete S2, which the script never asked
    to delete. The plan stays explicit: copy both sources' files, delete S1.
    """
    records = [
        _record("update", shared_id="T", metadata={"a": 1}),
        _record("move_files", from_shared_ids=["S1", "S2"], to_shared_id="T"),
        _record("delete", shared_id="S1"),
    ]

    rows = records_to_suggestions("run1", records)

    assert [(r.op, r.shared_id) for r in rows] == [
        ("update", "T"),
        ("move_files", "T"),
        ("delete", "S1"),
    ]
    assert rows[1].merge_source_ids == ["S1", "S2"]
    assert rows[2].merge_source_ids == []


def test_an_anchor_only_merge_moves_files_then_deletes_sources() -> None:
    """The recorded copy+delete merge has no property payload but is runnable.

    Its copy and its source deletes are derived from the ONE anchor row, and no
    update/create payload is produced.
    """
    files = [
        {"from_shared_id": "S1", "kind": "document", "originalname": "one.pdf"},
        {"from_shared_id": "S2", "kind": "document", "originalname": "two.pdf"},
    ]
    records = [
        _record("move_files", from_shared_ids=["S1", "S2"], to_shared_id="T", files=files),
        _record("delete", shared_id="S1"),
        _record("delete", shared_id="S2"),
    ]

    rows = records_to_suggestions("run1", records)

    assert [(r.op, r.shared_id, r.property_name) for r in rows] == [("update_and_delete", "T", None)]
    assert build_apply_updates(rows) == []
    assert partition_for_apply(rows) == ([], [])
    merges = merge_deletions(rows)
    assert merges == [("T", ["S1", "S2"])]
    assert merge_moves(merges) == [("T", ["S1", "S2"])]
    assert apply_units_total([], [], [], merges) == 2


def test_multi_group_merge_maps_each_target_to_its_own_sources() -> None:
    records = [
        _record("update", shared_id="T1", metadata={"a": 1}),
        _record("update", shared_id="T2", metadata={"b": 2}),
        _record("move_files", from_shared_ids=["S1"], to_shared_id="T1"),
        _record("move_files", from_shared_ids=["S2", "S3"], to_shared_id="T2"),
        _record("delete", shared_id="S1"),
        _record("delete", shared_id="S2"),
        _record("delete", shared_id="S3"),
    ]

    merges = merge_operations(records)
    assert list(merges) == ["T1", "T2"]
    assert merges["T1"].target_op == "update"
    assert merges["T1"].source_shared_ids == ("S1",)
    assert merges["T2"].source_shared_ids == ("S2", "S3")


def test_move_record_carries_the_files_the_review_shows() -> None:
    files = [
        {"from_shared_id": "S1", "kind": "document", "originalname": "source.pdf"},
        {"from_shared_id": "S1", "kind": "attachment", "originalname": "annex.html"},
    ]
    records = [
        _record("update", shared_id="T", metadata={"a": 1}),
        _record("move_files", from_shared_ids=["S1"], to_shared_id="T", files=files),
        _record("delete", shared_id="S1"),
    ]

    merge = merge_operations(records)["T"]
    rows = records_to_suggestions("run1", records)

    assert merge.files == tuple(files)
    assert all(row.merge_files == files for row in rows)


# --- no-op property rows ------------------------------------------------------


def _hydrated(
    *,
    shared_id: str = "T",
    op: str = "update_and_delete",
    property_name: str | None = "a",
    proposed: object = "same",
    current: object = "same",
    match: bool | None = True,
    merge_source_ids: list[str] | None = None,
    merge_files: list[dict] | None = None,
) -> Suggestion:
    return Suggestion(
        run_id="run1",
        shared_id=shared_id,
        op=op,  # type: ignore[arg-type]
        property_name=property_name,
        proposed_value=proposed,
        current_value=current,
        match=match,
        merge_source_ids=merge_source_ids or [],
        merge_files=merge_files or [],
        status=SuggestionStatus.PENDING,
    )


def test_unchanged_property_rows_are_dropped() -> None:
    rows = [
        _hydrated(property_name="same", match=True),
        _hydrated(property_name="changed", proposed="new", current="old", match=False),
        _hydrated(property_name="new", proposed="value", current=None, match=None),
    ]

    kept = drop_unchanged_property_rows(rows)

    assert [row.property_name for row in kept] == ["changed", "new"]


def test_a_merge_with_only_noop_writes_keeps_one_anchor_for_its_source_deletes() -> None:
    rows = [
        _hydrated(property_name="a", match=True, merge_source_ids=["S1"]),
        _hydrated(property_name="b", match=True, merge_source_ids=["S1"]),
    ]

    kept = drop_unchanged_property_rows(rows)

    assert [(row.op, row.property_name, row.proposed_value) for row in kept] == [("update_and_delete", None, None)]
    assert kept[0].merge_source_ids == ["S1"]


def test_a_merge_with_one_real_change_needs_no_anchor() -> None:
    rows = [
        _hydrated(property_name="a", match=True, merge_source_ids=["S1"]),
        _hydrated(property_name="b", proposed="new", current="old", match=False, merge_source_ids=["S1"]),
    ]

    kept = drop_unchanged_property_rows(rows)

    assert [(row.property_name, row.proposed_value) for row in kept] == [("b", "new")]


def test_an_anchor_only_merge_still_deletes_sources_and_moves_files() -> None:
    """The kept anchor writes nothing but IS the row the apply path reads.

    A merge whose target writes are all no-ops has no property payload; the
    source deletes and the file move must still be derived from the anchor.
    """
    rows = [
        _hydrated(property_name="a", match=True, merge_source_ids=["S1"]),
        _hydrated(property_name="b", match=True, merge_source_ids=["S1"]),
    ]

    anchor = drop_unchanged_property_rows(rows)

    assert build_apply_updates(anchor) == []
    property_rows, whole_entity_rows = partition_for_apply(anchor)
    assert (property_rows, whole_entity_rows) == ([], [])
    merges = merge_deletions(anchor)
    assert merges == [("T", ["S1"])]
    assert merge_moves(merges) == [("T", ["S1"])]


# --- merge suboperations in the review table ----------------------------------


def _operation(*subs: Suggestion) -> OperationReviewRow:
    return OperationReviewRow(
        shared_id="T",
        entity_title="Target",
        template_names=["Case"],
        ops=["update_and_delete"],
        properties=["a"],
        languages=[],
        proposal_count=len(subs),
        pending=len(subs),
        accepted=0,
        applied=0,
        reverted=0,
        discarded=0,
        edited=0,
        match_summary="mismatch",
        has_outcome=True,
        subs=list(subs),
    )


def test_merge_operation_shows_the_file_move_and_source_delete() -> None:
    merge = _hydrated(
        property_name="a",
        proposed="new",
        current="old",
        match=False,
        merge_source_ids=["S1"],
        merge_files=[
            {"from_shared_id": "S1", "kind": "document", "originalname": "source.pdf"},
            {"from_shared_id": "S1", "kind": "attachment", "originalname": "annex.html"},
        ],
    )

    row = view.operation_row(_operation(merge))
    labels = [sub["label"] for sub in row["subs"]]

    assert labels == [
        "a",
        "Primary document",
        "Supporting file",
        "Delete source entity",
    ]
    assert [sub["structural"] for sub in row["subs"]] == [False, True, True, True]
    assert all(not sub["selectable"] for sub in row["subs"][1:])
    assert row["subs"][1]["proposed"] == "source.pdf"
    assert row["subs"][2]["proposed"] == "annex.html"
    assert row["subs"][3]["caption"] == "S1"


def test_merge_operation_without_file_details_still_shows_the_move_and_delete() -> None:
    merge = _hydrated(
        property_name="a",
        proposed="new",
        current="old",
        match=False,
        merge_source_ids=["S1"],
    )

    row = view.operation_row(_operation(merge))
    labels = [sub["label"] for sub in row["subs"]]

    assert labels == ["a", "Uploaded files", "Delete source entity"]


def test_an_all_noop_merge_shows_only_its_file_and_delete_parts() -> None:
    """The anchor writes nothing, so the table must not render it as a change.

    Its status still drives the operation's own badge: an applied merge reads
    "runned" even though the only row left is its file move + source delete.
    """
    anchor = _hydrated(
        property_name=None,
        proposed=None,
        current=None,
        match=None,
        merge_source_ids=["S1"],
    ).model_copy(update={"status": SuggestionStatus.APPLIED})

    row = view.operation_row(_operation(anchor))

    assert [sub["label"] for sub in row["subs"]] == ["Uploaded files", "Delete source entity"]
    assert row["run_status"] == "runned"
    assert all(sub["structural"] for sub in row["subs"])


def test_an_anchor_only_merge_with_recorded_files_renders_copy_and_delete_cards() -> None:
    """No target write still shows the copy and the deletes, and stays runnable.

    The anchor is the ONLY row the apply path reads for this plan, so every
    structural card must carry its id: that is how the operation's Run reaches
    the file copy and the source deletes.
    """
    anchor = _hydrated(
        property_name=None,
        proposed=None,
        current=None,
        match=None,
        merge_source_ids=["S1", "S2"],
        merge_files=[
            {"from_shared_id": "S1", "kind": "document", "originalname": "one.pdf"},
            {"from_shared_id": "S2", "kind": "attachment", "originalname": "two.html"},
        ],
    ).model_copy(update={"id": 7})

    row = view.operation_row(_operation(anchor))

    assert [(sub["label"], sub["caption"]) for sub in row["subs"]] == [
        ("Primary document", "S1"),
        ("Supporting file", "S2"),
        ("Delete source entity", "S1"),
        ("Delete source entity", "S2"),
    ]
    assert all(sub["structural"] for sub in row["subs"])
    assert all(not sub["selectable"] for sub in row["subs"])
    assert all(sub["id"] == 7 and sub["shared_id"] == "T" for sub in row["subs"])


def _move_operation(*subs: Suggestion) -> OperationReviewRow:
    return OperationReviewRow(
        shared_id="T",
        entity_title="Target",
        template_names=["Case"],
        ops=["move_files"],
        properties=[],
        languages=[],
        proposal_count=len(subs),
        pending=len(subs),
        accepted=0,
        applied=0,
        reverted=0,
        discarded=0,
        edited=0,
        match_summary="n/a",
        has_outcome=False,
        subs=list(subs),
    )


def test_a_move_operation_shows_its_files_and_no_source_delete() -> None:
    """A file copy is one runnable row whose parts are the copied files.

    The row itself is not rendered as a property change (it writes no value);
    its structural file parts carry the row's id so the operation's Run still
    reaches it, and there is NO delete part — the sources are kept.
    """
    move = _hydrated(
        op="move_files",
        property_name=None,
        proposed=None,
        current=None,
        match=None,
        merge_source_ids=["S1"],
        merge_files=[
            {"from_shared_id": "S1", "kind": "document", "originalname": "source.pdf"},
            {"from_shared_id": "S1", "kind": "attachment", "originalname": "annex.html"},
        ],
    ).model_copy(update={"id": 42})

    row = view.operation_row(_move_operation(move))

    assert [sub["label"] for sub in row["subs"]] == ["Primary document", "Supporting file"]
    assert all(sub["structural"] for sub in row["subs"])
    assert all(sub["id"] == 42 for sub in row["subs"])
    assert all(not sub["selectable"] for sub in row["subs"])
    assert row["run_status"] == "pending"
    assert row["has_checkable"] is False
    assert row["border_color"] == view.OP_BORDER_COLORS["move_files"]
    assert [sub["caption"] for sub in row["subs"]] == ["S1", "S1"]


def test_a_newly_recorded_file_becomes_its_own_suboperation() -> None:
    """The reported expectation: a file added in Uwazi must be a NEW part.

    Refreshing the table rewrites the row's ``merge_files`` snapshot. Because
    the table renders one part per FILE, the added supporting file appears as
    its own suboperation (and the files already there keep their row keys, so
    nothing else in the operation is re-shuffled).
    """
    document = {"from_shared_id": "S1", "kind": "document", "file_id": "d1", "originalname": "source.pdf"}
    annex = {"from_shared_id": "S1", "kind": "attachment", "file_id": "a1", "originalname": "annex.html"}
    added = {"from_shared_id": "S1", "kind": "attachment", "file_id": "a2", "originalname": "report.txt"}
    before = _hydrated(
        op="move_files",
        property_name=None,
        proposed=None,
        current=None,
        match=None,
        merge_source_ids=["S1"],
        merge_files=[document, annex],
    ).model_copy(update={"id": 42})
    after = before.model_copy(update={"merge_files": [document, annex, added]})

    before_subs = view.operation_row(_move_operation(before))["subs"]
    after_subs = view.operation_row(_move_operation(after))["subs"]

    assert [sub["proposed"] for sub in before_subs] == ["source.pdf", "annex.html"]
    assert [sub["proposed"] for sub in after_subs] == ["source.pdf", "annex.html", "report.txt"]
    assert [sub["key"] for sub in after_subs[:2]] == [sub["key"] for sub in before_subs]
    assert after_subs[2]["label"] == "Supporting file"
    assert after_subs[2]["caption"] == "S1"
    assert after_subs[2]["structural"] is True
    assert len(after_subs) == len(before_subs) + 1


def test_a_replaced_file_row_follows_the_new_file_id() -> None:
    """Re-uploading a file mints a new id: the part is new, not silently retitled."""
    old = _hydrated(
        op="move_files",
        property_name=None,
        proposed=None,
        current=None,
        match=None,
        merge_source_ids=["S1"],
        merge_files=[{"from_shared_id": "S1", "kind": "attachment", "file_id": "a1", "originalname": "annex.html"}],
    ).model_copy(update={"id": 42})
    new = old.model_copy(
        update={
            "merge_files": [{"from_shared_id": "S1", "kind": "attachment", "file_id": "a2", "originalname": "annex.html"}]
        }
    )

    old_subs = view.operation_row(_move_operation(old))["subs"]
    new_subs = view.operation_row(_move_operation(new))["subs"]

    assert len(old_subs) == len(new_subs) == 1
    assert old_subs[0]["key"] != new_subs[0]["key"]


def test_each_recorded_move_keeps_its_own_file_rows_and_row_id() -> None:
    """Two fine-grained moves to one target are two sets of parts.

    Sharing one id would let a row-level Run/Discard reach only one of them.
    """
    first = _hydrated(
        op="move_files",
        property_name=None,
        proposed=None,
        current=None,
        match=None,
        merge_source_ids=["S1"],
    ).model_copy(update={"id": 42})
    second = _hydrated(
        op="move_files",
        property_name=None,
        proposed=None,
        current=None,
        match=None,
        merge_source_ids=["S2"],
    ).model_copy(update={"id": 43})

    row = view.operation_row(_move_operation(first, second))

    assert [(sub["caption"], sub["id"]) for sub in row["subs"]] == [("S1", 42), ("S2", 43)]
    assert all(sub["structural"] for sub in row["subs"])


def test_a_move_operation_without_recorded_files_shows_one_fallback_row() -> None:
    move = _hydrated(
        op="move_files",
        property_name=None,
        proposed=None,
        current=None,
        match=None,
        merge_source_ids=["S1", "S2"],
    )

    row = view.operation_row(_move_operation(move))

    assert [(sub["label"], sub["caption"]) for sub in row["subs"]] == [
        ("Uploaded files", "S1"),
        ("Uploaded files", "S2"),
    ]


# --- apply payloads -----------------------------------------------------------


def test_update_merge_row_is_an_update_payload_not_a_create() -> None:
    rows = [_suggestion(op="update_and_delete", property_name="excerpt", proposed="merged")]

    assert build_apply_updates(rows) == [{"shared_id": "s1", "language": None, "metadata": {"excerpt": "merged"}}]
    assert build_apply_creates(rows) == []


def test_create_merge_row_is_a_create_payload_not_an_update() -> None:
    rows = [
        _suggestion(
            shared_id="dry-created-0",
            op="create_and_delete",
            property_name="excerpt",
            proposed="new",
            template_name="Case",
        )
    ]

    assert build_apply_updates(rows) == []
    assert build_apply_creates(rows) == [
        {
            "title": "",
            "template_name": "Case",
            "metadata": {"excerpt": "new"},
            "language": None,
            "source_shared_id": "dry-created-0",
        }
    ]


def test_merge_deletions_dedupes_the_sources_of_one_target() -> None:
    rows = [
        _suggestion(op="update_and_delete", property_name="a", merge_source_ids=["S1", "S2"]),
        _suggestion(op="update_and_delete", property_name="b", merge_source_ids=["S1", "S2"]),
    ]

    assert merge_deletions(rows) == [("s1", ["S1", "S2"])]


def test_merge_deletions_keeps_target_order_and_ignores_other_ops() -> None:
    rows = [
        _suggestion(shared_id="T1", op="update_and_delete", property_name="a", merge_source_ids=["S1"]),
        _suggestion(shared_id="D", op="delete", property_name=None, merge_source_ids=["S9"]),
        _suggestion(shared_id="T0", op="create_and_delete", property_name="a", merge_source_ids=["S0"]),
    ]

    assert merge_deletions(rows) == [("T1", ["S1"]), ("T0", ["S0"])]


# --- merge file moves (apply path) -------------------------------------------


def test_merge_moves_uses_the_target_shared_id_for_an_update_merge() -> None:
    assert merge_moves([("T1", ["S1", "S2"])]) == [("T1", ["S1", "S2"])]


def test_merge_moves_resolves_a_create_placeholder_to_the_minted_id() -> None:
    moves = merge_moves([("dry-created-0", ["S1"])], created_ids={"dry-created-0": "real-42"})

    assert moves == [("real-42", ["S1"])]


def test_merge_moves_skips_a_target_whose_write_was_refused() -> None:
    merges = [("T1", ["S1"]), ("T2", ["S2"])]

    assert merge_moves(merges, failed_targets={"T1"}) == [("T2", ["S2"])]


def test_merge_moves_skips_a_refused_create_by_its_placeholder() -> None:
    merges = [("dry-created-0", ["S1"]), ("dry-created-1", ["S2"])]

    assert merge_moves(merges, failed_targets={"dry-created-0"}) == [("dry-created-1", ["S2"])]


def test_merge_moves_keeps_order_and_drops_targets_without_sources() -> None:
    merges = [("T2", ["S2"]), ("T1", []), ("T0", ["S0"])]

    assert merge_moves(merges) == [("T2", ["S2"]), ("T0", ["S0"])]


def test_merge_moves_returns_a_fresh_list_for_each_merge() -> None:
    sources = ["S1"]
    moves = merge_moves([("T1", sources)])

    moves[0][1].append("S2")
    assert sources == ["S1"]


# --- standalone file copies (apply path) -------------------------------------


def test_file_move_targets_dedupes_moves_for_one_target() -> None:
    rows = [
        _suggestion(shared_id="T", op="move_files", property_name=None, merge_source_ids=["S1", "S2"]),
        _suggestion(shared_id="T", op="move_files", property_name=None, merge_source_ids=["S2", "S3"]),
        _suggestion(shared_id="U", op="move_files", property_name=None, merge_source_ids=["S4"]),
    ]

    assert file_move_targets(rows) == [("T", ["S1", "S2", "S3"]), ("U", ["S4"])]


def test_file_move_targets_ignores_other_ops_a_self_copy_and_empty_sources() -> None:
    rows = [
        _suggestion(shared_id="T", op="update", property_name="a"),
        _suggestion(shared_id="T", op="move_files", property_name=None, merge_source_ids=["T"]),
        _suggestion(shared_id="U", op="move_files", property_name=None, merge_source_ids=[]),
        _suggestion(shared_id="V", op="move_files", property_name=None, merge_source_ids=["S1"]),
    ]

    assert file_move_targets(rows) == [("V", ["S1"])]


def test_a_file_copy_is_not_a_property_write_unit() -> None:
    rows = [
        _suggestion(shared_id="T", op="move_files", property_name=None, merge_source_ids=["S1"]),
        _suggestion(shared_id="T", op="update", property_name="a"),
    ]

    property_rows, whole_entity_rows = partition_for_apply(rows)

    assert [r.op for r in property_rows] == ["update"]
    assert whole_entity_rows == []
    # It still reaches the apply path that executes file copies.
    assert [r.op for r in pushable_rows(rows)] == ["move_files", "update"]


def test_apply_units_total_counts_one_unit_per_file_copy_target() -> None:
    assert apply_units_total([], [], [], file_moves=[("T", ["S1"]), ("U", ["S2", "S3"])]) == 2
    # A merge still counts its source deletions; a copy does not add sources.
    assert apply_units_total([], [], [], merges=[("T", ["S1", "S2"])]) == 2


def test_partition_routes_merge_rows_by_their_target_op() -> None:
    """An update-merge writes through the update path, a create-merge does not.

    A propertyless merge row is the all-no-op anchor: it carries merge_source_ids
    for merge_deletions but is not itself a write, so neither path claims it.
    """
    rows = [
        _suggestion(shared_id="u", op="update_and_delete", property_name="excerpt"),
        _suggestion(shared_id="dry-created-0", op="create_and_delete", property_name="excerpt"),
        _suggestion(shared_id="w", op="update_and_delete", property_name=None),
    ]

    property_rows, whole_entity_rows = partition_for_apply(rows)

    assert [r.shared_id for r in property_rows] == ["u"]
    assert [r.shared_id for r in whole_entity_rows] == ["dry-created-0"]
    # The anchor still reaches the merge logic that deletes the sources.
    assert [r.shared_id for r in pushable_rows(rows)] == ["u", "dry-created-0", "w"]


def test_pushable_rows_include_both_merge_ops() -> None:
    rows = [
        _suggestion(shared_id="u", op="update_and_delete", property_name="excerpt"),
        _suggestion(shared_id="dry-created-0", op="create_and_delete", property_name="excerpt"),
    ]

    assert [r.shared_id for r in pushable_rows(rows)] == ["u", "dry-created-0"]


# --- SQLite round-trip --------------------------------------------------------


def test_store_roundtrips_merge_source_ids(tmp_path) -> None:
    store = SqliteSuggestionStore(tmp_path / "suggestions.db")
    try:
        store.add_suggestions([_suggestion(op="update_and_delete", merge_source_ids=["S1", "S2"])])
        rows, total = store.query("run1", SuggestionFilter())
    finally:
        store.close()

    assert total == 1
    assert rows[0].merge_source_ids == ["S1", "S2"]


def test_store_roundtrips_merge_files(tmp_path) -> None:
    files = [{"from_shared_id": "S1", "kind": "document", "originalname": "source.pdf"}]
    suggestion = _suggestion(op="update_and_delete", merge_source_ids=["S1"]).model_copy(update={"merge_files": files})
    store = SqliteSuggestionStore(tmp_path / "suggestions.db")
    try:
        store.add_suggestions([suggestion])
        rows, total = store.query("run1", SuggestionFilter())
    finally:
        store.close()

    assert total == 1
    assert rows[0].merge_files == files


def test_file_operation_rows_returns_only_live_file_snapshots(tmp_path) -> None:
    """The targeted drift check reads exactly the rows that name source files."""
    files = [{"from_shared_id": "S1", "kind": "document", "file_id": "d1"}]
    merge = _suggestion(
        shared_id="T",
        op="update_and_delete",
        property_name="a",
        merge_source_ids=["S1"],
    ).model_copy(update={"merge_files": files})
    plain = _suggestion(shared_id="T", property_name="b")
    discarded = _suggestion(
        shared_id="D",
        op="update_and_delete",
        property_name="c",
        merge_source_ids=["S9"],
        status=SuggestionStatus.DISCARDED,
    )
    store = SqliteSuggestionStore(tmp_path / "suggestions.db")
    try:
        store.add_suggestions([merge, plain, discarded])
        rows = store.file_operation_rows("run1")
    finally:
        store.close()

    assert [(row.shared_id, row.property_name) for row in rows] == [("T", "a")]
    assert rows[0].merge_source_ids == ["S1"]
    assert rows[0].merge_files == files


def test_a_persisted_move_only_plan_reads_back_as_one_runnable_operation(tmp_path) -> None:
    """The reported regression, end to end through the real SQLite store.

    A plan whose only recorded work is a file copy must survive persistence and
    reach the table as ONE operation: the target, with the copied files as its
    (locked) parts and no delete part. Before the fix the move was dropped, so
    the plan rendered as info rows only and there was nothing to Run.
    """
    files = [
        {"from_shared_id": "S1", "kind": "document", "originalname": "source.pdf"},
        {"from_shared_id": "S1", "kind": "attachment", "originalname": "annex.html"},
    ]
    before = records_to_suggestions(
        "run1",
        [_record("move_files", from_shared_ids=["S1"], to_shared_id="T", files=files)],
    )
    store = SqliteSuggestionStore(tmp_path / "suggestions.db")
    try:
        store.add_suggestions(before)
        operations, total = store.operations("run1", SuggestionFilter())
    finally:
        store.close()

    assert total == 1
    assert len(operations) == 1
    operation = operations[0]
    assert (operation.shared_id, operation.ops) == ("T", ["move_files"])
    payload = view.operation_row(operation)
    assert payload["is_info"] is False
    assert [sub["label"] for sub in payload["subs"]] == ["Primary document", "Supporting file"]
    assert all(sub["structural"] for sub in payload["subs"])
    assert all(sub["id"] is not None for sub in payload["subs"])


def test_store_migrates_an_old_database_without_merge_source_ids(tmp_path) -> None:
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE suggestion (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            attempt INTEGER NOT NULL,
            shared_id TEXT NOT NULL,
            entity_title TEXT NOT NULL DEFAULT '',
            template_name TEXT,
            op TEXT NOT NULL,
            language TEXT,
            property_name TEXT,
            proposed_value TEXT,
            current_value TEXT,
            match_state INTEGER,
            status TEXT NOT NULL DEFAULT 'pending',
            edited_value TEXT,
            discard_reason TEXT,
            reviewed_at TEXT,
            created_at TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO suggestion (run_id, attempt, shared_id, entity_title, op, status, created_at) "
        "VALUES ('run1', 1, 's1', 'Title', 'delete', 'pending', '2026-01-01T00:00:00')"
    )
    conn.commit()
    conn.close()

    store = SqliteSuggestionStore(db)
    try:
        rows, total = store.query("run1", SuggestionFilter())
    finally:
        store.close()

    assert total == 1
    assert rows[0].merge_source_ids == []
    assert rows[0].merge_files == []


# --- review UI ----------------------------------------------------------------


def test_merge_entity_row_is_not_offered_the_in_place_revert() -> None:
    item = EntityReviewRow(
        shared_id="T",
        language=None,
        entity_title="Target",
        template_names=["Case"],
        ops=["update_and_delete"],
        properties=["a"],
        proposal_count=1,
        pending=0,
        accepted=0,
        applied=1,
        reverted=0,
        discarded=0,
        match_summary="exact",
        has_outcome=True,
    )

    row = entity_row(item)

    assert row["ops"] == "update_and_delete"
    assert row["revertable"] is False


def test_file_copy_entity_row_is_not_offered_the_in_place_revert() -> None:
    item = EntityReviewRow(
        shared_id="T",
        language=None,
        entity_title="Target",
        template_names=["Case"],
        ops=["move_files"],
        properties=[],
        proposal_count=1,
        pending=0,
        accepted=0,
        applied=1,
        reverted=0,
        discarded=0,
        match_summary="n/a",
        has_outcome=False,
    )

    row = entity_row(item)

    assert row["ops"] == "move_files"
    assert row["revertable"] is False


def test_run_confirmation_names_the_merge_action() -> None:
    _title, message = run_confirmation(1, ["update_and_delete"])

    assert "Merge (update + delete)" in message


def test_run_confirmation_does_not_promise_a_revert_for_a_file_copy() -> None:
    _title, message = run_confirmation(1, ["move_files"])

    assert "Add files" in message
    assert "stay on the target" in message
    assert "Revertable" not in message
