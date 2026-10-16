"""Isolated unit tests for the SQLite suggestion store + filter predicate (per AGENTS.md).

Real database on a ``tmp_path`` (local, offline, deterministic), real domain
models, plain assertions. No mocks, no monkeypatching, no network.
"""

import sqlite3
from datetime import datetime, timezone

import pytest

from uwazi_admin_agent.adapters.sqlite_suggestion_store import SqliteSuggestionStore, build_where
from uwazi_admin_agent.domain.llm_budget import LlmBudget, LlmPrice, LlmUsage
from uwazi_admin_agent.domain.suggestion import (
    PredictionAttempt,
    PredictionAttemptStatus,
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
    attempt: int = 1,
    run_id: str = "run1",
    proposed: object = "v",
) -> Suggestion:
    return Suggestion(
        run_id=run_id,
        attempt=attempt,
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


def test_build_where_always_scopes_to_run_and_attempt() -> None:
    sql, params = build_where(SuggestionFilter(), "run1", 2)
    assert sql == "run_id = ? AND attempt = ?"
    assert params == ["run1", 2]


def test_build_where_appends_only_set_predicates() -> None:
    sql, params = build_where(
        SuggestionFilter(
            status=SuggestionStatus.PENDING, op="update", property_name="excerpt", match="mismatch", text="foo"
        ),
        "run1",
        1,
    )
    assert params == ["run1", 1, "pending", "update", "excerpt", 0, "%foo%", "%foo%"]
    assert "status = ?" in sql
    assert "op = ?" in sql
    assert "property_name = ?" in sql
    assert "match_state = ?" in sql
    assert "LIKE ?" in sql


def test_build_where_accepted_only_uses_an_in_list() -> None:
    sql, params = build_where(SuggestionFilter(accepted_only=True), "run1", 1)
    assert sql.endswith("status IN (?, ?)")
    assert params == ["run1", 1, "accepted", "edited"]


def test_build_where_never_inlines_filter_values() -> None:
    """A filter value with SQL syntax stays a bound parameter (no injection)."""
    sql, params = build_where(SuggestionFilter(text="x'; DROP TABLE suggestion; --"), "run1", 1)
    assert "DROP TABLE" not in sql
    assert "x'; DROP TABLE suggestion; --" in params[2]


def test_build_where_omits_limit_and_offset() -> None:
    sql, _ = build_where(SuggestionFilter(limit=5, offset=100), "run1", 1)
    assert "LIMIT" not in sql.upper()
    assert "OFFSET" not in sql.upper()


# --- attempts ----------------------------------------------------------------


def test_attempt_round_trip(store: SqliteSuggestionStore) -> None:
    attempt = PredictionAttempt(
        run_id="run1",
        attempt=1,
        status=PredictionAttemptStatus.RUNNING,
        budget=_budget(),
        spent=LlmUsage(requests=3, input_tokens=100, output_tokens=50),
        entities_total=42,
        entities_done=7,
        entity_scope=["a", "b"],
        created_at=NOW,
    )
    store.create_attempt(attempt)
    loaded = store.get_attempt("run1", 1)
    assert loaded is not None
    assert loaded.status == PredictionAttemptStatus.RUNNING
    assert loaded.budget == _budget()
    assert loaded.spent == LlmUsage(requests=3, input_tokens=100, output_tokens=50)
    assert (loaded.entities_total, loaded.entities_done) == (42, 7)
    assert loaded.entity_scope == ["a", "b"]
    assert loaded.created_at == NOW


def test_whole_instance_attempt_has_none_scope(store: SqliteSuggestionStore) -> None:
    store.create_attempt(PredictionAttempt(run_id="run1", attempt=1, budget=_budget(), entity_scope=None))
    loaded = store.get_attempt("run1", 1)
    assert loaded is not None and loaded.entity_scope is None


def test_update_attempt_overwrites_progress_and_terminal_state(store: SqliteSuggestionStore) -> None:
    store.create_attempt(PredictionAttempt(run_id="run1", attempt=1, budget=_budget(), entities_total=10))
    store.update_attempt(
        PredictionAttempt(
            run_id="run1",
            attempt=1,
            status=PredictionAttemptStatus.ABORTED,
            budget=_budget(),
            spent=LlmUsage(requests=11),
            entities_total=10,
            entities_done=10,
            error="budget exceeded",
            finished_at=NOW,
        )
    )
    loaded = store.get_attempt("run1", 1)
    assert loaded is not None
    assert loaded.status == PredictionAttemptStatus.ABORTED
    assert loaded.entities_done == 10
    assert loaded.error == "budget exceeded"
    assert loaded.finished_at == NOW


def test_latest_and_list_attempts_are_newest_first(store: SqliteSuggestionStore) -> None:
    for number in (1, 2, 3):
        store.create_attempt(PredictionAttempt(run_id="run1", attempt=number, budget=_budget()))
    store.create_attempt(PredictionAttempt(run_id="other", attempt=9, budget=_budget()))
    assert store.latest_attempt("run1") is not None
    assert store.latest_attempt("run1").attempt == 3  # type: ignore[union-attr]
    assert [a.attempt for a in store.list_attempts("run1")] == [3, 2, 1]
    assert store.get_attempt("missing", 1) is None
    assert store.latest_attempt("missing") is None


# --- suggestions: upsert + query ---------------------------------------------


def test_add_suggestions_is_idempotent_on_the_identity(store: SqliteSuggestionStore) -> None:
    store.add_suggestions([_suggestion(proposed="first")])
    store.add_suggestions([_suggestion(proposed="second")])
    rows, total = store.query("run1", 1, SuggestionFilter())
    assert total == 1
    assert rows[0].proposed_value == "second"


def test_a_different_property_is_a_different_row(store: SqliteSuggestionStore) -> None:
    store.add_suggestions([_suggestion(property_name="a"), _suggestion(property_name="b")])
    _rows, total = store.query("run1", 1, SuggestionFilter())
    assert total == 2


def test_a_different_attempt_is_a_different_row(store: SqliteSuggestionStore) -> None:
    store.add_suggestions([_suggestion(attempt=1), _suggestion(attempt=2)])
    _rows, total = store.query("run1", 1, SuggestionFilter())
    assert total == 1
    _rows, total2 = store.query("run1", 2, SuggestionFilter())
    assert total2 == 1


def test_upsert_preserves_operator_review_state(store: SqliteSuggestionStore) -> None:
    """A re-run refreshes the proposal but must not un-discard a decision."""
    store.add_suggestions([_suggestion(proposed="v1")])
    rows, _ = store.query("run1", 1, SuggestionFilter())
    store.set_status([1], SuggestionStatus.DISCARDED, reason="wrong")
    store.add_suggestions([_suggestion(proposed="v2")])
    refreshed = store.query("run1", 1, SuggestionFilter())[0]
    assert refreshed[0].proposed_value == "v2"  # proposal refreshed
    assert refreshed[0].status == SuggestionStatus.DISCARDED  # decision preserved
    assert refreshed[0].discard_reason == "wrong"
    assert rows[0].status == SuggestionStatus.PENDING


def test_query_paginates_with_a_full_filter_total(store: SqliteSuggestionStore) -> None:
    store.add_suggestions([_suggestion(shared_id=f"s{n:03d}") for n in range(60)])
    first, total = store.query("run1", 1, SuggestionFilter(limit=25, offset=0))
    second, _ = store.query("run1", 1, SuggestionFilter(limit=25, offset=25))
    last, _ = store.query("run1", 1, SuggestionFilter(limit=25, offset=50))
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
    assert store.query("run1", 1, SuggestionFilter(property_name="x"))[1] == 2
    assert store.query("run1", 1, SuggestionFilter(match="mismatch"))[1] == 1
    assert store.query("run1", 1, SuggestionFilter(match="exact"))[1] == 1
    assert store.query("run1", 1, SuggestionFilter(match="new"))[1] == 2  # a3 + a4 (NULL match_state)
    assert store.query("run1", 1, SuggestionFilter(op="delete"))[1] == 1
    assert store.query("run1", 1, SuggestionFilter(text="beta"))[1] == 1  # title LIKE, case-insensitive
    assert store.query("run1", 1, SuggestionFilter(text="a4"))[1] == 1  # shared_id LIKE


def test_query_reports_rows_with_match_verdicts(store: SqliteSuggestionStore) -> None:
    store.add_suggestions([_suggestion(match=False), _suggestion(shared_id="s2", property_name="z", match=None)])
    rows, _ = store.query("run1", 1, SuggestionFilter())
    verdicts = {row.shared_id + str(row.property_name): row.match for row in rows}
    assert verdicts == {"s1excerpt": False, "s2z": None}


def test_get_entity_returns_only_that_entity(store: SqliteSuggestionStore) -> None:
    store.add_suggestions(
        [
            _suggestion(shared_id="s1", property_name="a"),
            _suggestion(shared_id="s1", property_name="b"),
            _suggestion(shared_id="s2", property_name="a"),
        ]
    )
    rows = store.get_entity("run1", 1, "s1")
    assert [r.property_name for r in rows] == ["a", "b"]


# --- status transitions ------------------------------------------------------


def test_set_status_marks_selected_rows_only(store: SqliteSuggestionStore) -> None:
    store.add_suggestions([_suggestion(shared_id="s1"), _suggestion(shared_id="s2")])
    rows, _ = store.query("run1", 1, SuggestionFilter())
    store.set_status([rows[0].id or 1], SuggestionStatus.ACCEPTED)  # type: ignore[attr-defined]
    after = store.query("run1", 1, SuggestionFilter())[0]
    assert after[0].status == SuggestionStatus.ACCEPTED
    assert after[0].reviewed_at is not None
    assert after[1].status == SuggestionStatus.PENDING


def test_set_status_records_edited_value_and_reason(store: SqliteSuggestionStore) -> None:
    store.add_suggestions([_suggestion()])
    store.set_status([1], SuggestionStatus.EDITED, edited_value="corrected")
    store.set_status([1], SuggestionStatus.DISCARDED, reason="not applicable")
    row = store.query("run1", 1, SuggestionFilter())[0][0]
    assert row.status == SuggestionStatus.DISCARDED
    assert row.discard_reason == "not applicable"


def test_set_status_by_filter_returns_the_affected_count(store: SqliteSuggestionStore) -> None:
    store.add_suggestions([_suggestion(shared_id=f"s{n}") for n in range(5)])
    affected = store.set_status_by_filter("run1", 1, SuggestionFilter(), SuggestionStatus.ACCEPTED)
    assert affected == 5
    assert store.counts("run1", 1)["accepted"] == 5


def test_set_status_by_filter_honors_the_filter(store: SqliteSuggestionStore) -> None:
    store.add_suggestions(
        [
            _suggestion(shared_id="a", property_name="x", match=False),
            _suggestion(shared_id="b", property_name="y", match=True),
        ]
    )
    affected = store.set_status_by_filter(
        "run1", 1, SuggestionFilter(match="mismatch"), SuggestionStatus.DISCARDED, "mismatch"
    )
    assert affected == 1
    counts = store.counts("run1", 1)
    assert counts["discarded"] == 1
    assert counts["pending"] == 1
    discarded = store.query("run1", 1, SuggestionFilter(status=SuggestionStatus.DISCARDED))[0]
    assert discarded[0].discard_reason == "mismatch"


def test_reset_statuses_undoes_discard_only(store: SqliteSuggestionStore) -> None:
    store.add_suggestions([_suggestion(shared_id="a"), _suggestion(shared_id="b")])
    first_rows, _ = store.query("run1", 1, SuggestionFilter())
    store.set_status([first_rows[0].id or 1], SuggestionStatus.DISCARDED, reason="x")  # type: ignore[attr-defined]
    store.set_status([first_rows[1].id or 2], SuggestionStatus.ACCEPTED)  # type: ignore[attr-defined]
    store.reset_statuses("run1", 1, [first_rows[0].id or 1, first_rows[1].id or 2])  # type: ignore[attr-defined]
    after = store.query("run1", 1, SuggestionFilter())[0]
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
    assert store.property_names("run1", 1) == ["alpha", "zebra"]


def test_shared_ids_projection_honors_the_filter_and_ignores_paging(store: SqliteSuggestionStore) -> None:
    store.add_suggestions([_suggestion(shared_id=f"s{n}", property_name="x") for n in range(60)])
    ids = store.shared_ids("run1", 1, SuggestionFilter(match="mismatch"))
    assert ids == []  # none mismatched
    only_three = store.shared_ids("run1", 1, SuggestionFilter(limit=3, offset=0))
    assert len(only_three) == 60  # paging is ignored: this is a projection


def test_shared_ids_filters_by_property(store: SqliteSuggestionStore) -> None:
    store.add_suggestions(
        [
            _suggestion(shared_id="a", property_name="x"),
            _suggestion(shared_id="b", property_name="y"),
            _suggestion(shared_id="c", property_name="x"),
        ]
    )
    assert store.shared_ids("run1", 1, SuggestionFilter(property_name="x")) == ["a", "c"]


def test_counts_groups_by_status_and_match(store: SqliteSuggestionStore) -> None:
    store.add_suggestions(
        [
            _suggestion(shared_id="a", match=True),
            _suggestion(shared_id="b", match=False),
            _suggestion(shared_id="c", match=None),
            _suggestion(shared_id="d", op="delete", property_name=None, match=None),
        ]
    )
    rows, _ = store.query("run1", 1, SuggestionFilter())
    store.set_status([rows[0].id or 1], SuggestionStatus.ACCEPTED)  # type: ignore[attr-defined]
    store.set_status([rows[1].id or 2], SuggestionStatus.DISCARDED)  # type: ignore[attr-defined]
    counts = store.counts("run1", 1)
    assert counts["total"] == 4
    assert counts["accepted"] == 1
    assert counts["discarded"] == 1
    assert counts["pending"] == 2
    assert counts["match_exact"] == 1
    assert counts["match_mismatch"] == 1
    assert counts["match_new"] == 2


def test_counts_on_an_empty_set(store: SqliteSuggestionStore) -> None:
    counts = store.counts("run1", 1)
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
    counts = store.counts("run1", 1)
    assert counts["total"] == 2
    assert counts["pending"] == 2


def test_counts_count_an_entity_once_per_status(store: SqliteSuggestionStore) -> None:
    """Many rows in one status are one entity in that status."""
    store.add_suggestions([_suggestion(shared_id="a", property_name=f"p{n}") for n in range(17)])
    store.set_status_by_filter("run1", 1, SuggestionFilter(), SuggestionStatus.ACCEPTED)
    counts = store.counts("run1", 1)
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
    counts = store.counts("run1", 1)
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
    page, _ = store.query("run1", 1, SuggestionFilter())
    store.set_status([page[1].id or 0], SuggestionStatus.ACCEPTED)  # type: ignore[attr-defined]
    counts = store.counts("run1", 1)
    assert counts["total"] == 1
    assert counts["accepted"] == 1
    assert counts["pending"] == 1
    assert counts["discarded"] == 0


# --- deletion ----------------------------------------------------------------


def test_delete_run_removes_suggestions_and_attempts(store: SqliteSuggestionStore) -> None:
    store.create_attempt(PredictionAttempt(run_id="run1", attempt=1, budget=_budget()))
    store.add_suggestions([_suggestion(run_id="run1"), _suggestion(run_id="run2")])
    store.delete_run("run1")
    assert store.query("run1", 1, SuggestionFilter())[1] == 0
    assert store.get_attempt("run1", 1) is None
    assert store.query("run2", 1, SuggestionFilter())[1] == 1


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
        rows, total = store.query("run1", 1, SuggestionFilter())
        assert total == 1
        assert rows[0].shared_id == "s1"
        assert rows[0].language is None
        assert rows[0].template_name is None
    finally:
        store.close()


def test_ensure_schema_is_idempotent_on_a_migrated_database(tmp_path) -> None:
    path = tmp_path / "legacy.db"
    _legacy_db(path)
    store = SqliteSuggestionStore(path)
    try:
        store.ensure_schema()
        store.ensure_schema()
        assert store.query("run1", 1, SuggestionFilter())[1] == 1
    finally:
        store.close()
