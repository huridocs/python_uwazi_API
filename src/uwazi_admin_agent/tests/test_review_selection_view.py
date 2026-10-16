"""Pure tests for the review table's selection label and selection pruning.

The entity table's action bar shows ``Run (N)`` for the rows the operator has
checked, and ``_reload_table`` replaces the rows without the table clearing its
checkbox selection. ``prune_selection`` is what keeps the count — and the Run
action, which reads the same selection — describing only the rows still visible
after a filter change or a page flip.

Isolated: no mocks, no network, literal inputs only.
"""

from __future__ import annotations

from uwazi_admin_agent.domain.suggestion import entity_language_key
from uwazi_admin_agent.drivers.web.review_view import prune_selection, run_button_label


def _row(shared_id: str, language: str | None = None, title: str = "Case 1") -> dict[str, str]:
    return {"row_key": entity_language_key(shared_id, language), "shared_id": shared_id, "title": title}


def test_run_button_label_counts_the_live_selection() -> None:
    """The button's text is the checked-row count, so Run (0) is the empty state."""
    assert run_button_label(0) == "Run (0)"
    assert run_button_label(1) == "Run (1)"
    assert run_button_label(37) == "Run (37)"


def test_prune_selection_keeps_everything_when_the_page_is_unchanged() -> None:
    """A poll reloads the same rows, so a checked row stays checked."""
    rows = [_row("s1"), _row("s2"), _row("s3")]
    selected = [rows[0], rows[2]]

    assert prune_selection(selected, rows) == [rows[0], rows[2]]


def test_prune_selection_drops_rows_left_on_a_previous_page() -> None:
    """Page flip: only the checked rows present on the NEW page survive."""
    page_one = [_row("s1"), _row("s2")]
    page_two = [_row("s3"), _row("s4")]

    assert prune_selection(page_one, page_two) == []


def test_prune_selection_drops_only_the_hidden_rows_after_a_filter_change() -> None:
    """A filter change keeps the checked rows the new filter still shows."""
    selected = [_row("s1"), _row("s2"), _row("s9")]
    remaining = [_row("s1", title="Case 9"), _row("s9", title="Case 3")]

    assert prune_selection(selected, remaining) == [selected[0], selected[2]]


def test_prune_selection_ignores_entries_without_a_row_key() -> None:
    """A malformed selection entry cannot be a visible row, so it is dropped."""
    rows = [_row("s1")]

    assert prune_selection([{"title": "no key"}, None, "s1", rows[0]], rows) == [rows[0]]


def test_prune_selection_keeps_one_language_without_dropping_its_sibling() -> None:
    """A translation's two rows are two selections: a page flip must not merge them."""
    selected = [_row("s1", "es")]
    remaining = [_row("s1", "es"), _row("s1", "en")]

    assert prune_selection(selected, remaining) == [selected[0]]


def test_prune_selection_drops_a_language_that_left_the_page() -> None:
    selected = [_row("s1", "es"), _row("s1", "en")]
    remaining = [_row("s1", "es")]

    assert prune_selection(selected, remaining) == [selected[0]]
