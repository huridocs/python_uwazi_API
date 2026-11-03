"""Isolated unit tests for the SQLite suggestion store + filter predicate (per AGENTS.md).

Real database on a ``tmp_path`` (local, offline, deterministic), real domain
models, plain assertions. No mocks, no monkeypatching, no network.
"""

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from uwazi_admin_agent.adapters.sqlite_suggestion_store import (
    SqliteSuggestionStore,
    build_where,
    latest_review_activity,
    operation_order_by,
)
from uwazi_admin_agent.domain.llm_budget import LlmBudget, LlmPrice, LlmUsage
from uwazi_admin_agent.domain.select_values import ALL_VALUES_REJECTED_REASON
from uwazi_admin_agent.domain.suggestion import (
    PredictionAttemptStatus,
    PredictionPass,
    Suggestion,
    SuggestionFilter,
    SuggestionStatus,
)

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
PRICE = LlmPrice(model="m", input_per_mtok=0.14, output_per_mtok=0.55)


def _budget() -> LlmBudget:
    return LlmBudget(max_cost=100.0, max_requests=100, price=PRICE)


@pytest.fixture()
def store(tmp_path) -> SqliteSuggestionStore:
    db = SqliteSuggestionStore(tmp_path / "suggestions.db")
    yield db
    db.close()


def _suggestion(
    *,
    shared_id: str = "s1",
    language: str | None = None,
    op: str = "update",
    property_name: str | None = "excerpt",
    status: SuggestionStatus = SuggestionStatus.PENDING,
    match: bool | None = True,
    title: str = "Title",
    run_id: str = "run1",
    proposed: object = "v",
) -> Suggestion:
    return Suggestion(
        run_id=run_id,
        shared_id=shared_id,
        language=language,
        entity_title=title,
        op=op,  # type: ignore[arg-type]
        property_name=property_name,
        proposed_value=proposed,
        current_value="v",
        match=match,
        status=status,
        created_at=NOW,
    )


# --- build_where -------------------------------------------------------------


def test_build_where_always_scopes_to_run() -> None:
    sql, params = build_where(SuggestionFilter(include_obsolete=True), "run1")
    assert sql == "run_id = ?"
    assert params == ["run1"]


def test_build_where_appends_only_set_predicates() -> None:
    sql, params = build_where(
        SuggestionFilter(
            status=SuggestionStatus.PENDING, op="update", property_name="excerpt", match="mismatch", text="foo"
        ),
        "run1",
    )
    assert params == ["run1", "pending", "update", "excerpt", 0, "%foo%", "%foo%"]
    assert "status = ?" in sql
    assert "op = ?" in sql
    assert "property_name = ?" in sql
    assert "match_state = ?" in sql
    assert "LIKE ?" in sql


def test_build_where_accepted_only_uses_an_in_list() -> None:
    sql, params = build_where(SuggestionFilter(accepted_only=True, include_obsolete=True), "run1")
    assert sql.endswith("status IN (?, ?)")
    assert params == ["run1", "accepted", "edited"]


def test_build_where_never_inlines_filter_values() -> None:
    """A filter value with SQL syntax stays a bound parameter (no injection)."""
    sql, params = build_where(SuggestionFilter(text="x'; DROP TABLE suggestion; --"), "run1")
    assert "DROP TABLE" not in sql
    assert any("x'; DROP TABLE suggestion; --" in str(value) for value in params)


def test_build_where_omits_limit_and_offset() -> None:
    sql, _ = build_where(SuggestionFilter(limit=5, offset=100), "run1")
    assert "LIMIT" not in sql.upper()
    assert "OFFSET" not in sql.upper()


# --- the run's prediction pass -----------------------------------------------


def test_pass_round_trip(store: SqliteSuggestionStore) -> None:
    pass_row = PredictionPass(
        run_id="run1",
        status=PredictionAttemptStatus.RUNNING,
        budget=_budget(),
        spent=LlmUsage(requests=3, input_tokens=100, output_tokens=50),
        entities_total=42,
        entities_done=7,
        entity_scope=["a", "b"],
        created_at=NOW,
    )
    store.save_pass(pass_row)
    loaded = store.get_pass("run1")
    assert loaded is not None
    assert loaded.status == PredictionAttemptStatus.RUNNING
    assert loaded.budget == _budget()
    assert loaded.spent == LlmUsage(requests=3, input_tokens=100, output_tokens=50)
    assert (loaded.entities_total, loaded.entities_done) == (42, 7)
    assert loaded.entity_scope == ["a", "b"]
    assert loaded.created_at == NOW


def test_whole_instance_pass_has_none_scope(store: SqliteSuggestionStore) -> None:
    store.save_pass(PredictionPass(run_id="run1", budget=_budget(), entity_scope=None))
    loaded = store.get_pass("run1")
    assert loaded is not None and loaded.entity_scope is None


def test_save_pass_overwrites_progress_and_terminal_state(store: SqliteSuggestionStore) -> None:
    store.save_pass(PredictionPass(run_id="run1", budget=_budget(), entities_total=10))
    store.save_pass(
        PredictionPass(
            run_id="run1",
            status=PredictionAttemptStatus.ABORTED,
            budget=_budget(),
            spent=LlmUsage(requests=11),
            entities_total=10,
            entities_done=10,
            error="budget exceeded",
            finished_at=NOW,
        )
    )
    loaded = store.get_pass("run1")
    assert loaded is not None
    assert loaded.status == PredictionAttemptStatus.ABORTED
    assert loaded.entities_done == 10
    assert loaded.error == "budget exceeded"
    assert loaded.finished_at == NOW


def test_get_pass_returns_the_runs_single_row(store: SqliteSuggestionStore) -> None:
    store.save_pass(PredictionPass(run_id="run1", budget=_budget()))
    store.save_pass(PredictionPass(run_id="other", budget=_budget()))
    assert store.get_pass("run1") is not None
    assert store.get_pass("missing") is None


# --- suggestions: upsert + query ---------------------------------------------


def test_add_suggestions_is_idempotent_on_the_identity(store: SqliteSuggestionStore) -> None:
    store.add_suggestions([_suggestion(proposed="first")])
    store.add_suggestions([_suggestion(proposed="second")])
    rows, total = store.query("run1", SuggestionFilter())
    assert total == 1
    assert rows[0].proposed_value == "second"


def test_a_different_property_is_a_different_row(store: SqliteSuggestionStore) -> None:
    store.add_suggestions([_suggestion(property_name="a"), _suggestion(property_name="b")])
    _rows, total = store.query("run1", SuggestionFilter())
    assert total == 2


def test_a_different_run_is_a_different_row(store: SqliteSuggestionStore) -> None:
    store.add_suggestions([_suggestion(run_id="run1"), _suggestion(run_id="run2")])
    _rows, total = store.query("run1", SuggestionFilter())
    assert total == 1
    _rows, total2 = store.query("run2", SuggestionFilter())
    assert total2 == 1


def test_upsert_preserves_operator_review_state(store: SqliteSuggestionStore) -> None:
    """A re-run refreshes the proposal but must not un-discard a decision."""
    store.add_suggestions([_suggestion(proposed="v1")])
    rows, _ = store.query("run1", SuggestionFilter())
    store.set_status([1], SuggestionStatus.DISCARDED, reason="wrong")
    store.add_suggestions([_suggestion(proposed="v2")])
    refreshed = store.query("run1", SuggestionFilter())[0]
    assert refreshed[0].proposed_value == "v2"  # proposal refreshed
    assert refreshed[0].status == SuggestionStatus.DISCARDED  # decision preserved
    assert refreshed[0].discard_reason == "wrong"
    assert rows[0].status == SuggestionStatus.PENDING


def test_query_paginates_with_a_full_filter_total(store: SqliteSuggestionStore) -> None:
    store.add_suggestions([_suggestion(shared_id=f"s{n:03d}") for n in range(60)])
    first, total = store.query("run1", SuggestionFilter(limit=25, offset=0))
    second, _ = store.query("run1", SuggestionFilter(limit=25, offset=25))
    last, _ = store.query("run1", SuggestionFilter(limit=25, offset=50))
    assert total == 60
    assert [len(first), len(second), len(last)] == [25, 25, 10]
    assert first[0].shared_id == "s000"
    assert second[0].shared_id == "s025"
    assert last[-1].shared_id == "s059"


def test_query_filters_by_status_op_property_match_and_text(store: SqliteSuggestionStore) -> None:
    store.add_suggestions(
        [
            _suggestion(shared_id="a1", property_name="x", match=True, title="Alpha"),
            _suggestion(shared_id="a2", property_name="x", match=False, title="Beta"),
            _suggestion(shared_id="a3", property_name="y", match=None, title="Gamma"),
            _suggestion(shared_id="a4", op="delete", property_name=None, match=None, title="Delta"),
        ]
    )
    assert store.query("run1", SuggestionFilter(property_name="x"))[1] == 2
    assert store.query("run1", SuggestionFilter(match="mismatch"))[1] == 1
    assert store.query("run1", SuggestionFilter(match="exact"))[1] == 1
    assert store.query("run1", SuggestionFilter(match="new"))[1] == 2  # a3 + a4 (NULL match_state)
    assert store.query("run1", SuggestionFilter(op="delete"))[1] == 1
    assert store.query("run1", SuggestionFilter(text="beta"))[1] == 1  # title LIKE, case-insensitive
    assert store.query("run1", SuggestionFilter(text="a4"))[1] == 1  # shared_id LIKE


def test_query_reports_rows_with_match_verdicts(store: SqliteSuggestionStore) -> None:
    store.add_suggestions([_suggestion(match=False), _suggestion(shared_id="s2", property_name="z", match=None)])
    rows, _ = store.query("run1", SuggestionFilter())
    verdicts = {row.shared_id + str(row.property_name): row.match for row in rows}
    assert verdicts == {"s1excerpt": False, "s2z": None}


# --- operations: modified column + sorting -----------------------------------


def test_operation_order_by_whitelists_the_sort_and_ties_on_first_id() -> None:
    assert operation_order_by("modified", True) == "modified_sort IS NULL, modified_sort DESC, first_id ASC"
    assert operation_order_by("title", False) == "title_sort ASC, first_id ASC"
    assert operation_order_by("unknown", False) == "first_id ASC"
    assert operation_order_by("recorded", True) == "first_id DESC"


def test_latest_review_activity_prefers_a_review_over_the_recording_time() -> None:
    recorded = _suggestion().model_copy(update={"created_at": datetime(2026, 1, 1, 10, tzinfo=timezone.utc)})
    reviewed = _suggestion().model_copy(
        update={
            "created_at": datetime(2026, 1, 1, 9, tzinfo=timezone.utc),
            "reviewed_at": datetime(2026, 1, 1, 11, tzinfo=timezone.utc),
        }
    )
    unreviewed = _suggestion().model_copy(update={"created_at": datetime(2026, 1, 1, 12, tzinfo=timezone.utc)})

    assert latest_review_activity([recorded, reviewed, unreviewed]) == unreviewed.created_at
    assert latest_review_activity([reviewed]) == reviewed.reviewed_at
    assert latest_review_activity([recorded]) == recorded.created_at
    assert latest_review_activity([]) is None


def test_operations_orders_by_modified_newest_first(store: SqliteSuggestionStore) -> None:
    older = _suggestion(shared_id="older").model_copy(
        update={"created_at": datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)}
    )
    newer = _suggestion(shared_id="newer").model_copy(
        update={"created_at": datetime(2026, 1, 1, 11, 0, tzinfo=timezone.utc)}
    )
    reviewed = _suggestion(shared_id="reviewed").model_copy(
        update={
            "created_at": datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc),
            "reviewed_at": datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
        }
    )
    store.add_suggestions([older, newer, reviewed])

    newest, total = store.operations("run1", SuggestionFilter(sort_by="modified", sort_desc=True))
    oldest, _ = store.operations("run1", SuggestionFilter(sort_by="modified", sort_desc=False))

    assert total == 3
    assert [row.shared_id for row in newest] == ["reviewed", "newer", "older"]
    assert [row.shared_id for row in oldest] == ["older", "newer", "reviewed"]
    assert newest[0].modified_at == datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def test_operations_orders_by_title_over_the_whole_set(store: SqliteSuggestionStore) -> None:
    store.add_suggestions(
        [
            _suggestion(shared_id="b", title="Beta"),
            _suggestion(shared_id="a", title="Alpha"),
        ]
    )

    rows, _ = store.operations("run1", SuggestionFilter(sort_by="title", sort_desc=False))

    assert [row.shared_id for row in rows] == ["a", "b"]


def test_get_entity_returns_only_that_entity(store: SqliteSuggestionStore) -> None:
    store.add_suggestions(
        [
            _suggestion(shared_id="s1", property_name="a"),
            _suggestion(shared_id="s1", property_name="b"),
            _suggestion(shared_id="s2", property_name="a"),
        ]
    )
    rows = store.get_entity("run1", "s1")
    assert [r.property_name for r in rows] == ["a", "b"]


# --- status transitions ------------------------------------------------------


def test_set_status_marks_selected_rows_only(store: SqliteSuggestionStore) -> None:
    store.add_suggestions([_suggestion(shared_id="s1"), _suggestion(shared_id="s2")])
    rows, _ = store.query("run1", SuggestionFilter())
    store.set_status([rows[0].id or 1], SuggestionStatus.ACCEPTED)  # type: ignore[attr-defined]
    after = store.query("run1", SuggestionFilter())[0]
    assert after[0].status == SuggestionStatus.ACCEPTED
    assert after[0].reviewed_at is not None
    assert after[1].status == SuggestionStatus.PENDING


def test_set_status_records_edited_value_and_reason(store: SqliteSuggestionStore) -> None:
    store.add_suggestions([_suggestion()])
    store.set_status([1], SuggestionStatus.EDITED, edited_value="corrected")
    store.set_status([1], SuggestionStatus.DISCARDED, reason="not applicable")
    row = store.query("run1", SuggestionFilter())[0][0]
    assert row.status == SuggestionStatus.DISCARDED
    assert row.discard_reason == "not applicable"


def test_set_status_by_filter_returns_the_affected_count(store: SqliteSuggestionStore) -> None:
    store.add_suggestions([_suggestion(shared_id=f"s{n}") for n in range(5)])
    affected = store.set_status_by_filter("run1", SuggestionFilter(), SuggestionStatus.ACCEPTED)
    assert affected == 5
    assert store.counts("run1")["accepted"] == 5


def test_set_status_by_filter_honors_the_filter(store: SqliteSuggestionStore) -> None:
    store.add_suggestions(
        [
            _suggestion(shared_id="a", property_name="x", match=False),
            _suggestion(shared_id="b", property_name="y", match=True),
        ]
    )
    affected = store.set_status_by_filter("run1", SuggestionFilter(match="mismatch"), SuggestionStatus.DISCARDED, "mismatch")
    assert affected == 1
    counts = store.counts("run1")
    assert counts["discarded"] == 1
    assert counts["pending"] == 1
    discarded = store.query("run1", SuggestionFilter(status=SuggestionStatus.DISCARDED))[0]
    assert discarded[0].discard_reason == "mismatch"


def test_reset_statuses_undoes_discard_only(store: SqliteSuggestionStore) -> None:
    store.add_suggestions([_suggestion(shared_id="a"), _suggestion(shared_id="b")])
    first_rows, _ = store.query("run1", SuggestionFilter())
    store.set_status([first_rows[0].id or 1], SuggestionStatus.DISCARDED, reason="x")  # type: ignore[attr-defined]
    store.set_status([first_rows[1].id or 2], SuggestionStatus.ACCEPTED)  # type: ignore[attr-defined]
    store.reset_statuses("run1", [first_rows[0].id or 1, first_rows[1].id or 2])  # type: ignore[attr-defined]
    after = store.query("run1", SuggestionFilter())[0]
    assert after[0].status == SuggestionStatus.PENDING
    assert after[0].discard_reason is None
    assert after[1].status == SuggestionStatus.ACCEPTED  # accept is not undone here


# --- projections -------------------------------------------------------------


def test_property_names_are_distinct_and_sorted(store: SqliteSuggestionStore) -> None:
    store.add_suggestions(
        [
            _suggestion(property_name="zebra"),
            _suggestion(shared_id="s2", property_name="alpha"),
            _suggestion(shared_id="s3", property_name="alpha"),
            _suggestion(shared_id="s4", op="delete", property_name=None),
        ]
    )
    assert store.property_names("run1") == ["alpha", "zebra"]


def test_shared_ids_projection_honors_the_filter_and_ignores_paging(store: SqliteSuggestionStore) -> None:
    store.add_suggestions([_suggestion(shared_id=f"s{n}", property_name="x") for n in range(60)])
    ids = store.shared_ids("run1", SuggestionFilter(match="mismatch"))
    assert ids == []  # none mismatched
    only_three = store.shared_ids("run1", SuggestionFilter(limit=3, offset=0))
    assert len(only_three) == 60  # paging is ignored: this is a projection


def test_selection_rows_runnable_only_excludes_non_runnable_statuses(store: SqliteSuggestionStore) -> None:
    """Whole-result selection may only include parts a Run can still write."""
    store.add_suggestions(
        [
            _suggestion(shared_id="pending", status=SuggestionStatus.PENDING),
            _suggestion(shared_id="accepted", status=SuggestionStatus.ACCEPTED),
            _suggestion(shared_id="edited", status=SuggestionStatus.EDITED),
            _suggestion(shared_id="reverted", status=SuggestionStatus.REVERTED),
            _suggestion(shared_id="applied", status=SuggestionStatus.APPLIED),
            _suggestion(shared_id="discarded", status=SuggestionStatus.DISCARDED),
            _suggestion(shared_id="obsolete", status=SuggestionStatus.OBSOLETE),
            _suggestion(shared_id="info", op="info", property_name=None),
        ]
    )

    every = store.selection_rows("run1", SuggestionFilter(include_obsolete=True))
    runnable = store.selection_rows("run1", SuggestionFilter(include_obsolete=True), runnable_only=True)

    assert {ref[1] for ref in every} == {
        "pending",
        "accepted",
        "edited",
        "reverted",
        "applied",
        "discarded",
        "obsolete",
    }
    assert {ref[1] for ref in runnable} == {"pending", "accepted", "edited", "reverted"}


def test_selection_rows_runnable_only_skips_a_reverted_recreating_op(store: SqliteSuggestionStore) -> None:
    """A reverted delete/merge names an old sharedId its undo re-created."""
    store.add_suggestions(
        [
            _suggestion(shared_id="update-reverted", status=SuggestionStatus.REVERTED),
            _suggestion(
                shared_id="delete-reverted",
                op="delete",
                property_name=None,
                status=SuggestionStatus.REVERTED,
            ),
            _suggestion(
                shared_id="merge-reverted",
                op="update_and_delete",
                property_name=None,
                status=SuggestionStatus.REVERTED,
            ),
            _suggestion(shared_id="delete-pending", op="delete", property_name=None),
        ]
    )

    runnable = store.selection_rows("run1", SuggestionFilter(), runnable_only=True)

    assert {ref[1] for ref in runnable} == {"update-reverted", "delete-pending"}


def test_sync_retires_a_reverted_delete_and_inserts_the_recreated_entity(store: SqliteSuggestionStore) -> None:
    """A recreated entity arrives under its NEW sharedId; the old row is retired."""
    old = _suggestion(
        shared_id="OLD",
        op="delete",
        property_name=None,
        status=SuggestionStatus.REVERTED,
    )
    store.add_suggestions([old])

    store.reconcile_suggestions(
        "run1",
        [_suggestion(shared_id="NEW", op="delete", property_name=None)],
    )

    rows = store.query("run1", SuggestionFilter(include_obsolete=True))[0]
    assert {(row.shared_id, row.status) for row in rows} == {
        ("OLD", SuggestionStatus.OBSOLETE),
        ("NEW", SuggestionStatus.PENDING),
    }


def test_sync_returns_a_reverted_merge_to_pending_with_fresh_sources(store: SqliteSuggestionStore) -> None:
    """Syncing re-reads a reverted merge from Uwazi, so it may run again."""
    old = _suggestion(
        shared_id="T",
        op="update_and_delete",
        property_name="a",
        proposed="same",
        status=SuggestionStatus.REVERTED,
    ).model_copy(update={"merge_source_ids": ["OLD-SOURCE"]})
    store.add_suggestions([old])

    store.reconcile_suggestions(
        "run1",
        [
            _suggestion(shared_id="T", op="update_and_delete", property_name="a", proposed="same").model_copy(
                update={"merge_source_ids": ["NEW-SOURCE"]}
            )
        ],
    )

    row = store.get_entity("run1", "T")[0]
    assert row.status is SuggestionStatus.PENDING
    assert row.merge_source_ids == ["NEW-SOURCE"]


def test_shared_ids_filters_by_property(store: SqliteSuggestionStore) -> None:
    store.add_suggestions(
        [
            _suggestion(shared_id="a", property_name="x"),
            _suggestion(shared_id="b", property_name="y"),
            _suggestion(shared_id="c", property_name="x"),
        ]
    )
    assert store.shared_ids("run1", SuggestionFilter(property_name="x")) == ["a", "c"]


def test_counts_groups_by_status_and_match(store: SqliteSuggestionStore) -> None:
    store.add_suggestions(
        [
            _suggestion(shared_id="a", match=True),
            _suggestion(shared_id="b", match=False),
            _suggestion(shared_id="c", match=None),
            _suggestion(shared_id="d", op="delete", property_name=None, match=None),
        ]
    )
    rows, _ = store.query("run1", SuggestionFilter())
    store.set_status([rows[0].id or 1], SuggestionStatus.ACCEPTED)  # type: ignore[attr-defined]
    store.set_status([rows[1].id or 2], SuggestionStatus.DISCARDED)  # type: ignore[attr-defined]
    counts = store.counts("run1")
    assert counts["total"] == 4
    assert counts["accepted"] == 1
    assert counts["discarded"] == 1
    assert counts["pending"] == 2
    assert counts["match_exact"] == 1
    assert counts["match_mismatch"] == 1
    assert counts["match_new"] == 2


def test_counts_on_an_empty_set(store: SqliteSuggestionStore) -> None:
    counts = store.counts("run1")
    assert counts["total"] == 0
    assert counts["pending"] == 0


def test_counts_report_entities_not_property_rows(store: SqliteSuggestionStore) -> None:
    """The cards must agree with the table below them, which holds one row per entity.

    One entity with 17 property rows is ONE entity: counting rows made the header
    read "Total 17" above a table showing a single entity.
    """
    store.add_suggestions(
        [_suggestion(shared_id="dry-created-0", property_name=f"p{n}") for n in range(17)]
        + [_suggestion(shared_id="s2", property_name="p0")]
    )
    counts = store.counts("run1")
    assert counts["total"] == 2
    assert counts["pending"] == 2


def test_counts_count_an_entity_once_per_status(store: SqliteSuggestionStore) -> None:
    """Many rows in one status are one entity in that status."""
    store.add_suggestions([_suggestion(shared_id="a", property_name=f"p{n}") for n in range(17)])
    store.set_status_by_filter("run1", SuggestionFilter(), SuggestionStatus.ACCEPTED)
    counts = store.counts("run1")
    assert counts["accepted"] == 1
    assert counts["pending"] == 0
    assert counts["total"] == 1


def test_counts_treat_languages_as_separate_entities(store: SqliteSuggestionStore) -> None:
    """A translation task is one entity PER LANGUAGE — the table's row identity."""
    store.add_suggestions(
        [
            _suggestion(shared_id="s1", language="en"),
            _suggestion(shared_id="s1", language="es"),
            _suggestion(shared_id="s1", language=None),  # the language-independent group
        ]
    )
    counts = store.counts("run1")
    assert counts["total"] == 3
    assert counts["pending"] == 3


def test_counts_report_a_group_in_each_status_it_spans(store: SqliteSuggestionStore) -> None:
    """Accepting one property of an entity leaves the group in BOTH cards.

    This is what picking that status in the filter dropdown shows, so a card and
    the table under it always agree.
    """
    store.add_suggestions(
        [
            _suggestion(shared_id="a", property_name="kept"),
            _suggestion(shared_id="a", property_name="taken"),
        ]
    )
    page, _ = store.query("run1", SuggestionFilter())
    store.set_status([page[1].id or 0], SuggestionStatus.ACCEPTED)  # type: ignore[attr-defined]
    counts = store.counts("run1")
    assert counts["total"] == 1
    assert counts["accepted"] == 1
    assert counts["pending"] == 1
    assert counts["discarded"] == 0


# --- deletion ----------------------------------------------------------------


def test_delete_run_removes_suggestions_and_the_pass_row(store: SqliteSuggestionStore) -> None:
    store.save_pass(PredictionPass(run_id="run1", budget=_budget()))
    store.add_suggestions([_suggestion(run_id="run1"), _suggestion(run_id="run2")])
    store.delete_run("run1")
    assert store.query("run1", SuggestionFilter())[1] == 0
    assert store.get_pass("run1") is None
    assert store.query("run2", SuggestionFilter())[1] == 1


# --- legacy migration ---------------------------------------------------------


# The suggestion table as an older build created it: no ``template_name``, no
# ``language``. Written out literally here so the migration is exercised against
# the real historical shape (not a current-schema table with columns dropped).
_LEGACY_SCHEMA = """
CREATE TABLE suggestion (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    shared_id TEXT NOT NULL,
    entity_title TEXT NOT NULL DEFAULT '',
    op TEXT NOT NULL,
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

CREATE UNIQUE INDEX suggestion_identity
    ON suggestion (run_id, attempt, shared_id, op, ifnull(property_name, ''));
"""


def _legacy_db(path) -> None:
    """Build a pre-``language`` database holding one suggestion row."""
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_LEGACY_SCHEMA)
        conn.execute(
            "INSERT INTO suggestion (run_id, attempt, shared_id, op, property_name, proposed_value) "
            "VALUES ('run1', 1, 's1', 'update', 'title', '\"v\"')"
        )
        conn.commit()
    finally:
        conn.close()


def test_store_opens_a_legacy_database_and_adds_the_missing_columns(tmp_path) -> None:
    """A pre-``language`` database migrates on construction instead of failing.

    The indexes name the migrated columns, so they must be created after the
    ALTERs: the reverse order aborts with "no such column: language", which took
    the whole runtime (and with it the wizard's validation step) down.
    """
    path = tmp_path / "legacy.db"
    _legacy_db(path)

    store = SqliteSuggestionStore(path)
    try:
        columns = {row["name"] for row in store._conn.execute("PRAGMA table_info(suggestion)")}  # noqa: SLF001
        assert {"template_name", "language"} <= columns
        indexes = {
            row["name"]
            for row in store._conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")  # noqa: SLF001
        }
        assert {"suggestion_identity", "suggestion_review", "suggestion_entity_language"} <= indexes
        # The legacy row survives, and reads NULL for both added columns — which
        # is exactly the language-independent group the grouped queries expect.
        rows, total = store.query("run1", SuggestionFilter())
        assert total == 1
        assert rows[0].shared_id == "s1"
        assert rows[0].language is None
        assert rows[0].template_name is None
    finally:
        store.close()


def test_the_attempt_migration_keeps_only_the_newest_attempt_per_run(tmp_path) -> None:
    """A pre-attempt-removal database becomes ONE live table per run.

    The old shape kept one row set per attempt; the live table is the newest
    attempt's rows, and the older ones are dropped. The ``attempt`` column and
    the ``prediction_attempt`` table are gone afterwards.
    """
    path = tmp_path / "old.db"
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_LEGACY_SCHEMA)
        conn.execute(
            "INSERT INTO suggestion (run_id, attempt, shared_id, op, property_name, proposed_value) "
            "VALUES ('run1', 1, 'old', 'update', 'title', '\"v\"')"
        )
        conn.execute(
            "INSERT INTO suggestion (run_id, attempt, shared_id, op, property_name, proposed_value) "
            "VALUES ('run1', 2, 'new', 'update', 'title', '\"v\"')"
        )
        conn.execute(
            "CREATE TABLE prediction_attempt ("
            "run_id TEXT NOT NULL, attempt INTEGER NOT NULL, status TEXT NOT NULL, "
            "budget TEXT NOT NULL, spent TEXT NOT NULL, entities_total INTEGER NOT NULL DEFAULT 0, "
            "entities_done INTEGER NOT NULL DEFAULT 0, entity_scope TEXT, error TEXT, "
            "created_at TEXT, finished_at TEXT, PRIMARY KEY (run_id, attempt))"
        )
        conn.execute(
            "INSERT INTO prediction_attempt (run_id, attempt, status, budget, spent) VALUES ('run1', 1, 'completed', ?, ?)",
            (json.dumps(_budget().model_dump(mode="json")), json.dumps(LlmUsage().model_dump(mode="json"))),
        )
        conn.execute(
            "INSERT INTO prediction_attempt (run_id, attempt, status, budget, spent) VALUES ('run1', 2, 'completed', ?, ?)",
            (json.dumps(_budget().model_dump(mode="json")), json.dumps(LlmUsage().model_dump(mode="json"))),
        )
        conn.commit()
    finally:
        conn.close()

    store = SqliteSuggestionStore(path)
    try:
        columns = {row["name"] for row in store._conn.execute("PRAGMA table_info(suggestion)")}  # noqa: SLF001
        assert "attempt" not in columns
        tables = {
            row["name"]
            for row in store._conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")  # noqa: SLF001
        }
        assert "prediction_attempt" not in tables
        assert "prediction_pass" in tables
        rows, total = store.query("run1", SuggestionFilter())
        assert total == 1
        assert rows[0].shared_id == "new"
        assert store.get_pass("run1") is not None
    finally:
        store.close()


def test_ensure_schema_is_idempotent_on_a_migrated_database(tmp_path) -> None:
    path = tmp_path / "legacy.db"
    _legacy_db(path)
    store = SqliteSuggestionStore(path)
    try:
        store.ensure_schema()
        store.ensure_schema()
        assert store.query("run1", SuggestionFilter())[1] == 1
    finally:
        store.close()


# --- multiselect rejected_values ---------------------------------------------


def _multiselect(
    *,
    proposed: list[str] | None = None,
    **kwargs: object,
) -> Suggestion:
    kwargs.setdefault("property_name", "keywords")
    return _suggestion(proposed=proposed or ["A", "B"], **kwargs)  # type: ignore[arg-type]


def _row_of(store: SqliteSuggestionStore, run_id: str = "run1") -> Suggestion:
    rows, _total = store.query(run_id, SuggestionFilter(include_obsolete=True))
    assert rows, "expected one row"
    return rows[0]


def test_set_rejected_values_persists_labels_and_status(store: SqliteSuggestionStore) -> None:
    store.add_suggestions([_multiselect()])
    store.set_rejected_values(1, ["B"], SuggestionStatus.DISCARDED, ALL_VALUES_REJECTED_REASON)
    row = _row_of(store)
    assert row.rejected_values == ["B"]
    assert row.status is SuggestionStatus.DISCARDED
    assert row.discard_reason == ALL_VALUES_REJECTED_REASON


def test_set_rejected_values_keeps_status_when_none(store: SqliteSuggestionStore) -> None:
    store.add_suggestions([_multiselect()])
    store.set_rejected_values(1, ["B"], None, None)
    row = _row_of(store)
    assert row.rejected_values == ["B"]
    assert row.status is SuggestionStatus.PENDING
    assert row.discard_reason is None


def test_set_status_edit_clears_rejections_but_apply_keeps_them(store: SqliteSuggestionStore) -> None:
    store.add_suggestions([_multiselect(), _multiselect(shared_id="s2", property_name="other", proposed=["C"])])
    rows, _total = store.query("run1", SuggestionFilter())
    ids = [row.id for row in rows]
    store.set_rejected_values(ids[1], ["B"])
    store.set_status([ids[0]], SuggestionStatus.EDITED, edited_value="fix")
    store.set_status([ids[1]], SuggestionStatus.ACCEPTED)
    fresh, _total = store.query("run1", SuggestionFilter())
    by_id = {row.id: row for row in fresh}
    assert by_id[ids[0]].rejected_values == []
    assert by_id[ids[1]].rejected_values == ["B"]


def test_restore_by_filter_clears_only_sentinel_rejections(store: SqliteSuggestionStore) -> None:
    store.add_suggestions(
        [
            _multiselect(shared_id="sentinel"),
            _multiselect(shared_id="manual", property_name="other", proposed=["C"]),
        ]
    )
    rows, _total = store.query("run1", SuggestionFilter())
    by_id = {row.shared_id: row for row in rows}
    store.set_rejected_values(by_id["sentinel"].id or 1, ["B"], SuggestionStatus.DISCARDED, ALL_VALUES_REJECTED_REASON)
    store.set_rejected_values(by_id["manual"].id or 2, [], SuggestionStatus.DISCARDED, "the operator refused this whole row")
    store.restore_by_filter("run1", SuggestionFilter())
    fresh, _total = store.query("run1", SuggestionFilter())
    after = {row.shared_id: row for row in fresh}
    assert after["sentinel"].rejected_values == []
    assert after["sentinel"].status is SuggestionStatus.PENDING
    # An explicit operator discard keeps the value-level choices the operator made.
    assert after["manual"].rejected_values == []
    assert after["manual"].status is SuggestionStatus.PENDING


def test_reset_statuses_clears_only_sentinel_rejections(store: SqliteSuggestionStore) -> None:
    store.add_suggestions([_multiselect(shared_id="sentinel"), _multiselect(shared_id="manual")])
    rows, _total = store.query("run1", SuggestionFilter())
    by_id = {row.shared_id: row for row in rows}
    store.set_rejected_values(by_id["sentinel"].id or 1, ["B"], SuggestionStatus.DISCARDED, ALL_VALUES_REJECTED_REASON)
    store.set_rejected_values(
        by_id["manual"].id or 2, ["A"], SuggestionStatus.DISCARDED, "the operator refused this whole row"
    )
    store.reset_statuses("run1", [by_id["sentinel"].id or 1, by_id["manual"].id or 2])
    fresh, _total = store.query("run1", SuggestionFilter())
    after = {row.shared_id: row for row in fresh}
    assert after["sentinel"].rejected_values == []
    assert after["manual"].rejected_values == ["A"]


def test_reconcile_keeps_rejections_for_a_same_proposal(store: SqliteSuggestionStore) -> None:
    store.add_suggestions([_multiselect(proposed=["A", "B"])])
    store.set_rejected_values(1, ["B"])
    summary = store.reconcile_suggestions("run1", [_multiselect(proposed=["A", "B"])])
    row = _row_of(store)
    assert row.rejected_values == ["B"]
    assert summary.refreshed == 0


def test_reconcile_clears_rejections_and_resets_sentinel_discard_on_a_changed_proposal(
    store: SqliteSuggestionStore,
) -> None:
    store.add_suggestions([_multiselect(proposed=["A", "B"])])
    store.set_rejected_values(1, ["A", "B"], SuggestionStatus.DISCARDED, ALL_VALUES_REJECTED_REASON)
    summary = store.reconcile_suggestions("run1", [_multiselect(proposed=["A", "C"])])
    row = _row_of(store)
    assert row.rejected_values == []
    # The synthetic discard only meant "nothing left to keep": the new proposal
    # goes back into review.
    assert row.status is SuggestionStatus.PENDING
    assert row.discard_reason is None
    assert summary.refreshed == 1


def test_reconcile_keeps_a_real_discard_on_a_changed_proposal(store: SqliteSuggestionStore) -> None:
    store.add_suggestions([_multiselect(proposed=["A", "B"])])
    store.set_rejected_values(1, ["A", "B"], SuggestionStatus.DISCARDED, "the operator refused this whole row")
    store.reconcile_suggestions("run1", [_multiselect(proposed=["A", "C"])])
    row = _row_of(store)
    assert row.rejected_values == []
    assert row.status is SuggestionStatus.DISCARDED
    assert row.discard_reason == "the operator refused this whole row"


def test_add_suggestions_over_existing_identity_keeps_stored_rejections(store: SqliteSuggestionStore) -> None:
    store.add_suggestions([_multiselect(proposed=["A", "B"])])
    store.set_rejected_values(1, ["B"])
    store.add_suggestions([_multiselect(proposed=["A", "B"])])
    row = _row_of(store)
    assert row.rejected_values == ["B"]
