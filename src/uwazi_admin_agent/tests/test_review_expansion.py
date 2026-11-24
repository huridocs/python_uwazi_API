"""Pure tests for the review table's page-wide expansion mode.

"Expand all" and "Collapse all" are page-wide choices, not a list of the
current page's row keys: the mode they set is what ``_reload_table`` re-applies,
so pressing Next after Expand all lands on a fully expanded page (and Collapse
all carries across pages the same way).

Isolated: no mocks, no network, literal inputs only.
"""

from __future__ import annotations

from uwazi_admin_agent.drivers.web.review_view import expanded_keys_for, expansion_mode


def _row(key: str) -> dict[str, str]:
    return {"row_key": key, "title": key}


def test_expansion_mode_reports_a_fully_expanded_page() -> None:
    rows = [_row("a"), _row("b")]

    assert expansion_mode(rows, {"a", "b"}) == "all"


def test_expansion_mode_reports_a_fully_collapsed_page() -> None:
    rows = [_row("a"), _row("b")]

    assert expansion_mode(rows, set()) == "none"


def test_expansion_mode_reports_a_per_row_state_as_mixed() -> None:
    rows = [_row("a"), _row("b")]

    assert expansion_mode(rows, {"a"}) == "mixed"


def test_expansion_mode_of_an_empty_page_is_none() -> None:
    """No rows means nothing can be uniformly expanded."""
    assert expansion_mode([], set()) == "none"


def test_expanded_keys_for_all_expands_the_next_pages_rows() -> None:
    """Expand all then Next: the new page's keys, not the old page's."""
    next_page = [_row("c"), _row("d")]

    assert expanded_keys_for(next_page, "all", {"a", "b"}) == {"c", "d"}


def test_expanded_keys_for_none_collapses_the_next_pages_rows() -> None:
    """Collapse all then Next: nothing expanded, even if a row was open before."""
    next_page = [_row("c"), _row("d")]

    assert expanded_keys_for(next_page, "none", {"a", "c"}) == set()


def test_expanded_keys_for_mixed_keeps_the_open_rows_of_the_same_page() -> None:
    """A poll reload of the same page must not close a manually opened row."""
    rows = [_row("a"), _row("b")]

    assert expanded_keys_for(rows, "mixed", {"b"}) == {"b"}


def test_expanded_keys_for_mixed_drops_the_previous_pages_keys() -> None:
    """A single open row is page-local: flipping pages starts collapsed."""
    next_page = [_row("c"), _row("d")]

    assert expanded_keys_for(next_page, "mixed", {"a"}) == set()
