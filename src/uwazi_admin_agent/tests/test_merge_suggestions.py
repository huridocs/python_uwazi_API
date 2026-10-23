"""Isolated unit tests for merge detection and the two merge ops (per AGENTS.md).

Pure transforms plus a real SQLite round-trip on ``tmp_path``: script records in,
reviewable suggestions / apply payloads out. No mocks, no network, no instance.

The contract under test: a scripted merge (target update/create + file move +
source deletes) is ONE review row — the merge op on the TARGET, with the source
ids riding along — and its apply payloads are the ordinary update/create
payloads plus one bulk delete of the sources.
"""

import sqlite3
from datetime import datetime, timezone

from uwazi_admin_agent.adapters.sqlite_suggestion_store import SqliteSuggestionStore
from uwazi_admin_agent.domain.suggestion import (
    TITLE_PROPERTY,
    EntityReviewRow,
    Suggestion,
    SuggestionFilter,
    SuggestionStatus,
)
from uwazi_admin_agent.domain.suggestion_scoring import merge_operations, records_to_suggestions
from uwazi_admin_agent.drivers.web.review_view import entity_row, run_confirmation
from uwazi_admin_agent.use_cases.apply_suggestions_use_case import (
    build_apply_creates,
    build_apply_updates,
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


def test_a_move_without_a_target_write_is_not_a_merge() -> None:
    records = [
        _record("move_files", from_shared_ids=["S1"], to_shared_id="T"),
        _record("delete", shared_id="S1"),
    ]

    rows = records_to_suggestions("run1", records)

    assert [(r.op, r.shared_id) for r in rows] == [("delete", "S1")]
    assert rows[0].merge_source_ids == []


def test_an_update_without_a_move_keeps_its_delete_apart() -> None:
    records = [
        _record("update", shared_id="T", metadata={"a": 1}),
        _record("delete", shared_id="S1"),
    ]

    rows = records_to_suggestions("run1", records)

    assert [(r.op, r.shared_id) for r in rows] == [("update", "T"), ("delete", "S1")]


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


def test_partition_routes_merge_rows_by_their_target_op() -> None:
    rows = [
        _suggestion(shared_id="u", op="update_and_delete", property_name="excerpt"),
        _suggestion(shared_id="dry-created-0", op="create_and_delete", property_name="excerpt"),
        _suggestion(shared_id="w", op="update_and_delete", property_name=None),
    ]

    property_rows, whole_entity_rows = partition_for_apply(rows)

    assert [r.shared_id for r in property_rows] == ["u"]
    assert [r.shared_id for r in whole_entity_rows] == ["dry-created-0", "w"]


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


def test_run_confirmation_names_the_merge_action() -> None:
    _title, message = run_confirmation(1, ["update_and_delete"])

    assert "Merge (update + delete)" in message
