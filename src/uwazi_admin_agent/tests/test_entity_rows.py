"""Isolated unit tests for the store's per-entity aggregate rows (per AGENTS.md).

Real SQLite on a ``tmp_path``, real domain models, plain assertions. No mocks,
no network. This is the review table's read: ONE row per affected entity.
"""

from datetime import datetime, timezone

import pytest

from uwazi_admin_agent.adapters.sqlite_suggestion_store import SqliteSuggestionStore
from uwazi_admin_agent.domain.suggestion import EntityReviewRow, Suggestion, SuggestionFilter, SuggestionStatus

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def store(tmp_path) -> SqliteSuggestionStore:
    db = SqliteSuggestionStore(tmp_path / "suggestions.db")
    yield db
    db.close()


def _suggestion(
    *,
    shared_id: str = "s1",
    op: str = "update",
    property_name: str | None = "excerpt",
    status: SuggestionStatus = SuggestionStatus.PENDING,
    match: bool | None = True,
    title: str = "Title",
    proposed: object = "v",
    run_id: str = "run1",
) -> Suggestion:
    return Suggestion(
        run_id=run_id,
        shared_id=shared_id,
        entity_title=title,
        op=op,  # type: ignore[arg-type]
        property_name=property_name,
        proposed_value=proposed,
        match=match,
        status=status,
        created_at=NOW,
    )


def _add(store: SqliteSuggestionStore, *rows: Suggestion) -> None:
    store.add_suggestions(list(rows))


# --- one row per entity -------------------------------------------------------


def test_one_entity_with_update_and_delete_yields_one_row(store: SqliteSuggestionStore) -> None:
    _add(
        store,
        _suggestion(op="update", property_name="excerpt"),
        _suggestion(shared_id="s1", op="delete", property_name=None),
    )
    rows, total = store.entity_rows("run1", SuggestionFilter())

    assert total == 1
    assert len(rows) == 1
    item = rows[0]
    assert isinstance(item, EntityReviewRow)
    assert item.shared_id == "s1"
    assert item.ops == ["update", "delete"]  # distinct ops, first-appearance order
    assert item.properties == ["excerpt"]
    assert item.proposal_count == 2
    assert item.pending == 2
    assert item.accepted == 0
    assert item.applied == 0
    assert item.discarded == 0


def test_rows_group_by_shared_id_in_first_appearance_order(store: SqliteSuggestionStore) -> None:
    _add(
        store,
        _suggestion(shared_id="b", property_name="excerpt", title="Bee"),
        _suggestion(shared_id="a", title="Ay"),
        _suggestion(shared_id="b", op="publish", property_name=None, title="Bee"),
    )
    rows, total = store.entity_rows("run1", SuggestionFilter())
    assert [r.shared_id for r in rows] == ["b", "a"]
    assert total == 2
    assert rows[0].ops == ["update", "publish"]
    assert rows[0].entity_title == "Bee"


def test_properties_are_distinct_and_first_appearance_ordered(store: SqliteSuggestionStore) -> None:
    _add(
        store,
        _suggestion(property_name="z"),
        _suggestion(property_name="a"),
        _suggestion(shared_id="s2", property_name="z"),
        _suggestion(shared_id="s2", property_name="z", proposed="second"),  # same identity: upsert, no dup
    )
    rows, _ = store.entity_rows("run1", SuggestionFilter())
    assert rows[0].properties == ["z", "a"]
    assert rows[0].proposal_count == 2


# --- match_summary ------------------------------------------------------------


def test_match_summary_single_verdicts(store: SqliteSuggestionStore) -> None:
    _add(store, _suggestion(match=True), _suggestion(shared_id="s2", match=False, title="T2"))
    rows, _ = store.entity_rows("run1", SuggestionFilter())
    assert {r.shared_id: r.match_summary for r in rows} == {"s1": "exact", "s2": "mismatch"}


def test_match_summary_mixed_when_verdicts_disagree(store: SqliteSuggestionStore) -> None:
    _add(store, _suggestion(match=True), _suggestion(property_name="other", match=False))
    rows, _ = store.entity_rows("run1", SuggestionFilter())
    assert rows[0].match_summary == "mixed"


def test_match_summary_new_when_no_current_value(store: SqliteSuggestionStore) -> None:
    """A proposal with no current value is 'new' (match=None on a property row)."""
    _add(store, _suggestion(match=None))
    rows, _ = store.entity_rows("run1", SuggestionFilter())
    assert rows[0].match_summary == "new"


def test_match_summary_na_for_whole_entity_ops_only(store: SqliteSuggestionStore) -> None:
    _add(store, _suggestion(op="delete", property_name=None, match=None))
    rows, _ = store.entity_rows("run1", SuggestionFilter())
    assert rows[0].match_summary == "n/a"
    assert rows[0].has_outcome is False


# --- has_outcome + status sums ------------------------------------------------


def test_has_outcome_reflects_a_proposed_property_value(store: SqliteSuggestionStore) -> None:
    _add(store, _suggestion(op="delete", property_name=None))
    rows, _ = store.entity_rows("run1", SuggestionFilter())
    assert rows[0].has_outcome is False

    _add(store, _suggestion(property_name="excerpt", proposed="computed"))
    rows, _ = store.entity_rows("run1", SuggestionFilter())
    assert rows[0].has_outcome is True


def test_status_sums_per_entity(store: SqliteSuggestionStore) -> None:
    _add(
        store,
        _suggestion(status=SuggestionStatus.ACCEPTED),
        _suggestion(status=SuggestionStatus.APPLIED, property_name="other"),
        _suggestion(status=SuggestionStatus.DISCARDED, property_name="third"),
    )
    rows, _ = store.entity_rows("run1", SuggestionFilter())
    item = rows[0]
    assert (item.pending, item.accepted, item.applied, item.discarded, item.proposal_count) == (0, 1, 1, 1, 3)


# --- shared_ids subset + paging ------------------------------------------------


def test_entity_rows_respect_the_shared_ids_subset(store: SqliteSuggestionStore) -> None:
    _add(store, _suggestion(shared_id="a"), _suggestion(shared_id="b"), _suggestion(shared_id="c"))
    rows, total = store.entity_rows("run1", SuggestionFilter(shared_ids=["a", "c"]))
    assert [r.shared_id for r in rows] == ["a", "c"]
    assert total == 2


def test_an_empty_shared_ids_list_matches_nothing(store: SqliteSuggestionStore) -> None:
    _add(store, _suggestion(shared_id="a"))
    rows, total = store.entity_rows("run1", SuggestionFilter(shared_ids=[]))
    assert rows == []
    assert total == 0


def test_entity_rows_page_and_total(store: SqliteSuggestionStore) -> None:
    store.add_suggestions([_suggestion(shared_id=f"s{n:02d}") for n in range(30)])
    rows, total = store.entity_rows("run1", SuggestionFilter(limit=10, offset=0))
    assert len(rows) == 10
    assert total == 30
    page2, total2 = store.entity_rows("run1", SuggestionFilter(limit=10, offset=10))
    assert len(page2) == 10
    assert total2 == 30
    assert {r.shared_id for r in rows}.isdisjoint({r.shared_id for r in page2})


def test_entity_rows_for_another_run_are_excluded(store: SqliteSuggestionStore) -> None:
    _add(store, _suggestion(), _suggestion(run_id="run2", shared_id="s2"))
    rows, total = store.entity_rows("run1", SuggestionFilter())
    assert total == 1
    assert rows[0].shared_id == "s1"
