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


def _statuses(store: SqliteSuggestionStore) -> dict[str, SuggestionStatus]:
    rows, _total = store.query("r", SuggestionFilter(include_obsolete=True, limit=100))
    return {str(row.property_name): row.status for row in rows}


def test_reconcile_preserves_and_resets_exactly_the_agreed_statuses() -> None:
    store = SqliteSuggestionStore(Path(":memory:"))
    try:
        store.add_suggestions(
            [
                _suggestion("same", "unchanged"),
                _suggestion("accepted", "old", status=SuggestionStatus.ACCEPTED),
                _suggestion("edited", "old", status=SuggestionStatus.EDITED, edited_value='[{"value": "mine"}]'),
                _suggestion("applied_missing", "old", status=SuggestionStatus.APPLIED),
                _suggestion("obsolete_returning", "old", status=SuggestionStatus.OBSOLETE),
                _suggestion("obsolete_missing", "old", status=SuggestionStatus.OBSOLETE),
            ]
        )
        summary = store.reconcile_suggestions(
            "r",
            [
                _suggestion("same", "unchanged"),
                _suggestion("accepted", "new"),
                _suggestion("edited", "new", status=SuggestionStatus.EDITED),
                _suggestion("obsolete_returning", "old"),
                _suggestion("new_row", "fresh"),
            ],
        )
        statuses = _statuses(store)
        assert statuses["same"] is SuggestionStatus.PENDING
        assert statuses["accepted"] is SuggestionStatus.PENDING
        assert statuses["edited"] is SuggestionStatus.EDITED
        assert statuses["applied_missing"] is SuggestionStatus.APPLIED
        assert statuses["obsolete_returning"] is SuggestionStatus.PENDING
        assert statuses["obsolete_missing"] is SuggestionStatus.OBSOLETE
        assert statuses["new_row"] is SuggestionStatus.PENDING
        assert summary.inserted == 1
        assert summary.obsolete == 1
        assert summary.kept_applied == 1
        assert summary.reappeared == 1
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

        rows, _total = store.query("r", SuggestionFilter(include_obsolete=True, limit=100))
        statuses = {(row.op, row.shared_id): row.status for row in rows}
        assert statuses[("info", "info-0")] is SuggestionStatus.OBSOLETE
        assert summary.obsolete == 1
    finally:
        store.close()


def test_obsolete_rows_are_hidden_unless_requested() -> None:
    store = SqliteSuggestionStore(Path(":memory:"))
    try:
        store.add_suggestions(
            [
                _suggestion("visible", "v"),
                _suggestion("retired", "r", status=SuggestionStatus.OBSOLETE),
            ]
        )
        visible, visible_total = store.query("r", SuggestionFilter(limit=100))
        all_rows, all_total = store.query("r", SuggestionFilter(include_obsolete=True, limit=100))
        assert visible_total == 1
        assert [row.property_name for row in visible] == ["visible"]
        assert all_total == 2
        assert {row.property_name for row in all_rows} == {"visible", "retired"}
    finally:
        store.close()
