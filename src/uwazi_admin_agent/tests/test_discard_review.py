"""Isolated unit tests for the review UI's discard action + discard filter (per AGENTS.md).

Real SQLite on a ``tmp_path`` and real domain models, plain assertions: no
mocks, no network, no Uwazi instance. The discard is the operator's "the end
user does not want this executed" — it marks the operation's rows, and the
table can then be filtered by that state.
"""

from datetime import datetime, timezone

import pytest

from uwazi_admin_agent.adapters.sqlite_suggestion_store import SqliteSuggestionStore
from uwazi_admin_agent.domain.suggestion import EntityReviewRow, Suggestion, SuggestionFilter, SuggestionStatus
from uwazi_admin_agent.drivers.web.review_view import (
    bulk_discard_label,
    counterpart_filter,
    discard_confirmation,
    entity_is_discarded,
    entity_row,
    filter_from_controls,
    run_status,
)

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


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
    run_id: str = "run1",
    attempt: int = 1,
) -> Suggestion:
    return Suggestion(
        run_id=run_id,
        attempt=attempt,
        shared_id=shared_id,
        entity_title="Title",
        op=op,  # type: ignore[arg-type]
        language=language,
        property_name=property_name,
        proposed_value="v",
        match=True,
        status=status,
        created_at=NOW,
    )


def _entity_row(*, proposal_count: int = 2, discarded: int = 0, applied: int = 0) -> EntityReviewRow:
    return EntityReviewRow(
        shared_id="s1",
        language=None,
        entity_title="Title",
        template_names=["Judgment"],
        ops=["update"],
        properties=["excerpt"],
        proposal_count=proposal_count,
        pending=max(proposal_count - discarded - applied, 0),
        accepted=0,
        applied=applied,
        reverted=0,
        discarded=discarded,
        match_summary="exact",
        has_outcome=True,
    )


# --- the filter control --------------------------------------------------------


def test_filter_from_controls_reads_the_discarded_tri_state() -> None:
    """The 'Discarded' select: any / not discarded / discarded."""
    assert filter_from_controls().discarded is None
    assert filter_from_controls(discarded="yes").discarded is True
    assert filter_from_controls(discarded="no").discarded is False


def test_filter_from_controls_keeps_the_other_filters_intact() -> None:
    f = filter_from_controls(status="pending", discarded="no", op="update")
    assert f.status is SuggestionStatus.PENDING
    assert f.discarded is False
    assert f.op == "update"


def test_the_status_and_discarded_filters_exclude_each_other() -> None:
    """'discarded' plus 'not discarded' would be an empty table nobody can read."""
    assert counterpart_filter("discarded") == "status"
    assert counterpart_filter("status") == "discarded"
    assert counterpart_filter("op") == ""


# --- the row contract ----------------------------------------------------------


def test_an_fully_discarded_operation_is_discarded() -> None:
    assert entity_is_discarded(_entity_row(proposal_count=2, discarded=2)) is True


def test_a_partially_discarded_operation_is_not_discarded() -> None:
    """Work remains, so the operator still has to see it under 'not discarded'."""
    assert entity_is_discarded(_entity_row(proposal_count=3, discarded=1)) is False


def test_run_status_reads_discarded_for_a_refused_operation() -> None:
    assert run_status(_entity_row(proposal_count=2, discarded=2)) == ("discarded", "red")


def test_run_status_stays_pending_when_only_some_rows_are_discarded() -> None:
    assert run_status(_entity_row(proposal_count=3, discarded=1)) == ("pending", "grey")


def test_entity_row_carries_the_discard_flag_for_the_actions_cell() -> None:
    """The Actions cell swaps its Discard button for the Undo one."""
    assert entity_row(_entity_row(proposal_count=2, discarded=2))["discarded"] is True
    assert entity_row(_entity_row(proposal_count=2, discarded=1))["discarded"] is False


def test_bulk_discard_label_carries_the_selection_count() -> None:
    assert bulk_discard_label(3) == "Discard (3)"


def test_discard_confirmation_says_the_rows_will_not_be_executed() -> None:
    title, message = discard_confirmation(2)
    assert title == "Discard 2 operations"
    assert "NOT be executed" in message


# --- the store: marking an operation as discarded --------------------------------


def test_discard_by_filter_marks_the_operation_rows_and_keeps_the_reason(store: SqliteSuggestionStore) -> None:
    store.add_suggestions([_suggestion(property_name="a"), _suggestion(property_name="b")])

    moved = store.discard_by_filter(
        "run1",
        1,
        SuggestionFilter(entity_languages=[("s1", None)]),
        "the end user does not want this",
    )

    assert moved == 2
    rows, _ = store.query("run1", 1, SuggestionFilter())
    assert [row.status for row in rows] == [SuggestionStatus.DISCARDED, SuggestionStatus.DISCARDED]
    assert rows[0].discard_reason == "the end user does not want this"


def test_discard_by_filter_leaves_the_already_written_rows_alone(store: SqliteSuggestionStore) -> None:
    """A discard says what must NOT be executed; un-writing is the revert's job."""
    store.add_suggestions(
        [
            _suggestion(property_name="a", status=SuggestionStatus.APPLIED),
            _suggestion(property_name="b"),
        ]
    )

    moved = store.discard_by_filter("run1", 1, SuggestionFilter(entity_languages=[("s1", None)]), None)

    assert moved == 1
    rows, _ = store.query("run1", 1, SuggestionFilter())
    assert rows[0].status is SuggestionStatus.APPLIED
    assert rows[1].status is SuggestionStatus.DISCARDED


def test_discarding_one_language_leaves_its_sibling_alone(store: SqliteSuggestionStore) -> None:
    """A translation task's two languages are two operations."""
    store.add_suggestions([_suggestion(language="es"), _suggestion(language="en")])

    store.discard_by_filter("run1", 1, SuggestionFilter(entity_languages=[("s1", "es")]), None)

    rows, _ = store.query("run1", 1, SuggestionFilter())
    by_language = {row.language: row.status for row in rows}
    assert by_language == {"es": SuggestionStatus.DISCARDED, "en": SuggestionStatus.PENDING}


def test_restore_by_filter_brings_the_operation_back(store: SqliteSuggestionStore) -> None:
    store.add_suggestions([_suggestion(property_name="a")])
    store.discard_by_filter("run1", 1, SuggestionFilter(entity_languages=[("s1", None)]), "not now")

    restored = store.restore_by_filter("run1", 1, SuggestionFilter(entity_languages=[("s1", None)]))

    assert restored == 1
    rows, _ = store.query("run1", 1, SuggestionFilter())
    assert rows[0].status is SuggestionStatus.PENDING
    assert rows[0].discard_reason is None


def test_restore_by_filter_touches_only_the_discarded_rows(store: SqliteSuggestionStore) -> None:
    store.add_suggestions(
        [
            _suggestion(property_name="a", status=SuggestionStatus.DISCARDED),
            _suggestion(property_name="b", status=SuggestionStatus.ACCEPTED),
        ]
    )

    restored = store.restore_by_filter("run1", 1, SuggestionFilter(entity_languages=[("s1", None)]))

    assert restored == 1
    rows, _ = store.query("run1", 1, SuggestionFilter())
    assert rows[0].status is SuggestionStatus.PENDING
    assert rows[1].status is SuggestionStatus.ACCEPTED


# --- the store: filtering by the discard state ------------------------------------


def test_the_discarded_filter_reads_the_discarded_rows(store: SqliteSuggestionStore) -> None:
    store.add_suggestions(
        [
            _suggestion(shared_id="a", property_name="x", status=SuggestionStatus.DISCARDED),
            _suggestion(shared_id="b", property_name="x"),
        ]
    )

    rows, total = store.query("run1", 1, SuggestionFilter(discarded=True))

    assert total == 1
    assert [row.shared_id for row in rows] == ["a"]


def test_the_not_discarded_filter_reads_the_rest(store: SqliteSuggestionStore) -> None:
    store.add_suggestions(
        [
            _suggestion(shared_id="a", property_name="x", status=SuggestionStatus.DISCARDED),
            _suggestion(shared_id="b", property_name="x"),
        ]
    )

    rows, total = store.query("run1", 1, SuggestionFilter(discarded=False))

    assert total == 1
    assert [row.shared_id for row in rows] == ["b"]


def test_the_entity_table_filters_the_operations_by_their_discard_state(store: SqliteSuggestionStore) -> None:
    """The table's rows are operations: a discarded one drops out of 'not discarded'."""
    store.add_suggestions(
        [
            _suggestion(shared_id="a", property_name="x", status=SuggestionStatus.DISCARDED),
            _suggestion(shared_id="b", property_name="x"),
        ]
    )

    discarded_rows, discarded_total = store.entity_rows("run1", 1, SuggestionFilter(discarded=True))
    kept_rows, kept_total = store.entity_rows("run1", 1, SuggestionFilter(discarded=False))

    assert (discarded_total, [row.shared_id for row in discarded_rows]) == (1, ["a"])
    assert (kept_total, [row.shared_id for row in kept_rows]) == (1, ["b"])


def test_a_discarded_operation_reads_as_discarded_in_the_table(store: SqliteSuggestionStore) -> None:
    store.add_suggestions([_suggestion(property_name="x")])
    store.discard_by_filter("run1", 1, SuggestionFilter(entity_languages=[("s1", None)]), None)

    rows, _ = store.entity_rows("run1", 1, SuggestionFilter())

    assert rows[0].discarded == rows[0].proposal_count
    assert entity_is_discarded(rows[0]) is True
