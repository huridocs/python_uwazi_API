"""Isolated in-memory test of the review-table reconciliation SQL (per AGENTS.md).

The pure rules are covered in ``test_review_refresh.py``; this file exercises
the store's in-place UPDATE/INSERT merge against an in-memory SQLite database.
No files, network, or mocks.
"""

from pathlib import Path

from uwazi_admin_agent.adapters.sqlite_suggestion_store import SqliteSuggestionStore
from uwazi_admin_agent.domain.suggestion import Suggestion, SuggestionFilter, SuggestionStatus


def _suggestion(property_name: str, proposed: str, **kwargs: object) -> Suggestion:
    base: dict[str, object] = {
        "run_id": "r",
        "shared_id": "entity-1",
        "op": "update",
        "property_name": property_name,
        "proposed_value": [{"value": proposed}],
        "current_value": [{"value": "before"}],
        "match": False,
        "status": SuggestionStatus.PENDING,
    }
    base.update(kwargs)
    return Suggestion(**base)


def _rows(store: SqliteSuggestionStore) -> list[Suggestion]:
    rows, _total = store.query("r", SuggestionFilter(limit=100))
    return rows


def _statuses(store: SqliteSuggestionStore) -> dict[str, SuggestionStatus]:
    return {str(row.property_name): row.status for row in _rows(store)}


def test_reconcile_preserves_and_resets_exactly_the_agreed_statuses() -> None:
    store = SqliteSuggestionStore(Path(":memory:"))
    try:
        store.add_suggestions(
            [
                _suggestion("same", "unchanged"),
                _suggestion("accepted", "old", status=SuggestionStatus.ACCEPTED),
                _suggestion("edited", "old", status=SuggestionStatus.EDITED, edited_value='[{"value": "mine"}]'),
                _suggestion("applied_missing", "old", status=SuggestionStatus.APPLIED),
                _suggestion("dropped", "old"),
            ]
        )
        summary = store.reconcile_suggestions(
            "r",
            [
                _suggestion("same", "unchanged"),
                _suggestion("accepted", "new"),
                _suggestion("edited", "new", status=SuggestionStatus.EDITED),
                _suggestion("new_row", "fresh"),
            ],
        )
        statuses = _statuses(store)
        assert statuses["same"] is SuggestionStatus.PENDING
        assert statuses["accepted"] is SuggestionStatus.PENDING
        assert statuses["edited"] is SuggestionStatus.EDITED
        # An APPLIED write really happened: it stays as the run's history.
        assert statuses["applied_missing"] is SuggestionStatus.APPLIED
        # Every other stale row is GONE — no retired placeholder in the table.
        assert "dropped" not in statuses
        assert statuses["new_row"] is SuggestionStatus.PENDING
        assert summary.inserted == 1
        assert summary.removed == 1
        assert summary.kept_applied == 1
    finally:
        store.close()


def test_a_stale_row_is_deleted_so_one_entity_keeps_one_row() -> None:
    """The reported bug: a re-plan left the old ``skipped`` row beside the proposal.

    A skipped record carries ``language=None`` while the new proposal carries the
    document's language, so the two landed in DIFFERENT review groups: the table
    showed the entity twice (once "not processed", once "matched"), and the
    header cards counted the dead row.
    """
    store = SqliteSuggestionStore(Path(":memory:"))
    try:
        store.add_suggestions(
            [
                Suggestion(
                    run_id="r",
                    shared_id="entity-1",
                    op="skipped",
                    property_name=None,
                    language=None,
                    skip_reason="no_segmentation",
                    status=SuggestionStatus.PENDING,
                )
            ]
        )
        store.reconcile_suggestions(
            "r",
            [
                _suggestion(
                    "document_ref",
                    "A/HRC/37/NGO/188",
                    op="matched",
                    language="en",
                    detail_key="document_ref",
                    match=True,
                )
            ],
        )

        rows = _rows(store)
        assert [(row.shared_id, row.language, row.op) for row in rows] == [("entity-1", "en", "matched")]
    finally:
        store.close()


def test_a_matched_row_that_becomes_a_change_becomes_that_update_row() -> None:
    """A refresh that turns "already in Uwazi" into a real change keeps ONE row.

    The store identities differ (``matched`` vs ``update``), so an unpaired
    merge would delete the matched row and insert a fresh update: the table
    would still show ONE change, but its row id and the operator's status on it
    would be lost.
    """
    store = SqliteSuggestionStore(Path(":memory:"))
    try:
        store.add_suggestions(
            [
                _suggestion(
                    "excerpt",
                    "before",
                    op="matched",
                    detail_key="excerpt",
                    match=True,
                )
            ]
        )
        summary = store.reconcile_suggestions("r", [_suggestion("excerpt", "new")])

        rows, total = store.query("r", SuggestionFilter(limit=100))
        assert total == 1
        assert rows[0].op == "update"
        assert rows[0].property_name == "excerpt"
        assert rows[0].detail_key is None
        assert rows[0].status is SuggestionStatus.PENDING
        assert rows[0].proposed_value == [{"value": "new"}]
        assert summary.inserted == 0
        assert summary.removed == 0
    finally:
        store.close()


def test_an_update_that_now_matches_becomes_the_matched_row_in_place() -> None:
    store = SqliteSuggestionStore(Path(":memory:"))
    try:
        store.add_suggestions([_suggestion("excerpt", "same", status=SuggestionStatus.ACCEPTED)])
        incoming = _suggestion(
            "excerpt",
            "same",
            op="matched",
            detail_key="excerpt",
            match=True,
            status=SuggestionStatus.PENDING,
        )
        summary = store.reconcile_suggestions("r", [incoming])

        rows, total = store.query("r", SuggestionFilter(limit=100))
        assert total == 1
        assert rows[0].op == "matched"
        assert rows[0].status is SuggestionStatus.PENDING
        assert summary.inserted == 0
        assert summary.removed == 0
    finally:
        store.close()


def test_a_property_that_stops_being_proposed_is_removed_entirely() -> None:
    """Dropping a property from the script drops its row: nothing is parked.

    A leftover matched/update row kept a stale property visible in the Property
    filter and in the accuracy figure of a plan that no longer writes it.
    """
    store = SqliteSuggestionStore(Path(":memory:"))
    try:
        store.add_suggestions([_suggestion("excerpt", "new"), _suggestion("summary", "text")])
        summary = store.reconcile_suggestions("r", [_suggestion("excerpt", "new")])

        rows = _rows(store)
        assert [row.property_name for row in rows] == ["excerpt"]
        assert summary.removed == 1
        assert summary.inserted == 0
    finally:
        store.close()


def test_a_scoped_refresh_retires_a_stale_info_row() -> None:
    """Info rows name no entity, so a scoped pass still re-decides them.

    Without this, a "moved N files" summary from an earlier pass stayed visible
    forever on every scoped refresh (``info-N`` is never in the entity scope).
    """
    store = SqliteSuggestionStore(Path(":memory:"))
    try:
        store.add_suggestions(
            [
                _suggestion("a", "v"),
                Suggestion(
                    run_id="r",
                    shared_id="info-0",
                    op="info",
                    property_name=None,
                    proposed_value="Moved 2 files",
                    status=SuggestionStatus.PENDING,
                ),
            ]
        )

        summary = store.reconcile_suggestions("r", [_suggestion("a", "v")], entity_scope=["entity-1"])

        rows = _rows(store)
        assert [(row.op, row.shared_id) for row in rows] == [("update", "entity-1")]
        assert summary.removed == 1
    finally:
        store.close()


def test_the_table_holds_every_row_the_run_really_has() -> None:
    """No status is hidden by default: a deleted stale row leaves nothing behind.

    The default filter used to hide OBSOLETE rows, which is what made the
    Op/Status counters (which counted them) disagree with the table.
    """
    store = SqliteSuggestionStore(Path(":memory:"))
    try:
        store.add_suggestions(
            [
                _suggestion("visible", "v"),
                _suggestion("applied", "a", status=SuggestionStatus.APPLIED),
                _suggestion("discarded", "d", status=SuggestionStatus.DISCARDED),
            ]
        )
        rows, total = store.query("r", SuggestionFilter(limit=100))
        assert total == 3
        assert {row.property_name for row in rows} == {"visible", "applied", "discarded"}
    finally:
        store.close()
