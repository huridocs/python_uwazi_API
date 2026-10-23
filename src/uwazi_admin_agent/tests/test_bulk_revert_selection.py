"""Isolated tests for the selected-rows bulk Revert (per AGENTS.md).

No network, no mocks of behavior under test: the pieces verified are the PURE
selection helpers (``unique_entities``, ``bulk_revert_label``) and the REAL
``SqliteSuggestionStore``'s scoped status flip — the seams the bulk button is
built from. The restore loop itself is the per-entity path already covered by
``test_revert_entity_flow.py``.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from uwazi_admin_agent.adapters.sqlite_suggestion_store import SqliteSuggestionStore
from uwazi_admin_agent.domain.suggestion import Suggestion, SuggestionFilter
from uwazi_admin_agent.drivers.web.review_view import bulk_revert_label, unique_entities

# --- pure selection helpers --------------------------------------------------


def test_unique_entities_collapses_language_rows() -> None:
    selection = [("s1", "es"), ("s1", "en"), ("s2", None)]
    assert unique_entities(selection) == ["s1", "s2"]


def test_unique_entities_keeps_first_appearance_and_drops_empty() -> None:
    selection = [("b", None), ("", None), ("a", "es"), ("b", "en")]
    assert unique_entities(selection) == ["b", "a"]


def test_bulk_revert_label_carries_the_count() -> None:
    assert bulk_revert_label(0) == "Revert (0)"
    assert bulk_revert_label(7) == "Revert (7)"


# --- scoped status flip ------------------------------------------------------


def _suggestion(run_id: str, shared_id: str, language: str, status: str) -> Suggestion:
    return Suggestion(
        run_id=run_id,
        shared_id=shared_id,
        op="update",
        language=language,
        property_name="excerpt",
        proposed_value="x",
        status=status,  # type: ignore[arg-type]
    )


def test_mark_applied_reverted_for_flips_only_named_entities_all_their_languages() -> None:
    """The bulk flip covers exactly the reverted sharedIds, across languages."""
    with tempfile.TemporaryDirectory() as tmp:
        store = SqliteSuggestionStore(Path(tmp) / "suggestions.db")
        store.add_suggestions(
            [
                _suggestion("run1", "s1", "es", "applied"),
                _suggestion("run1", "s1", "en", "applied"),
                _suggestion("run1", "s2", "es", "applied"),  # NOT selected
                _suggestion("run1", "s3", "en", "applied"),
            ]
        )
        moved = store.mark_applied_reverted_for("run1", ["s3", "s1"])
        assert moved == 3
        rows, _total = store.entity_rows("run1", SuggestionFilter(limit=100))
        states = {(row.shared_id, row.language): (row.reverted, row.applied) for row in rows}
        assert states[("s1", "es")] == (1, 0)
        assert states[("s1", "en")] == (1, 0)
        assert states[("s3", "en")] == (1, 0)
        # The unselected entity keeps its APPLIED status (still written).
        assert states[("s2", "es")] == (0, 1)
        store.close()


def test_mark_applied_reverted_for_empty_selection_moves_nothing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = SqliteSuggestionStore(Path(tmp) / "suggestions.db")
        store.add_suggestions([_suggestion("run1", "s1", "es", "applied")])
        assert store.mark_applied_reverted_for("run1", []) == 0
        rows, _total = store.entity_rows("run1", SuggestionFilter(limit=100))
        assert rows[0].applied == 1
        assert rows[0].reverted == 0
        store.close()


def test_mark_applied_reverted_for_does_not_touch_pending_rows() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = SqliteSuggestionStore(Path(tmp) / "suggestions.db")
        store.add_suggestions([_suggestion("run1", "s1", "es", "pending")])
        assert store.mark_applied_reverted_for("run1", ["s1"]) == 0
        rows, _total = store.entity_rows("run1", SuggestionFilter(limit=100))
        assert rows[0].pending == 1
        assert rows[0].reverted == 0
        store.close()
