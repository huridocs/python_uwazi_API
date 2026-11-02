"""Isolated unit tests for the filter SQL builder and the control translation.

The builder is pure; the semantics check below runs the generated clause against
a real in-memory SQLite database (offline, deterministic, no fixtures).
"""

from __future__ import annotations

import sqlite3

from uwazi_admin_agent.adapters.sqlite_suggestion_store import build_where
from uwazi_admin_agent.domain.suggestion import SuggestionFilter
from uwazi_admin_agent.drivers.web.review_view import filter_from_controls


def _max_paren_depth(sql: str) -> int:
    """Deepest nesting of parentheses in ``sql`` (the tree SQLite will build)."""
    depth = deepest = 0
    for char in sql:
        if char == "(":
            depth += 1
            deepest = max(deepest, depth)
        elif char == ")":
            depth -= 1
    return deepest


def test_language_filter_adds_an_in_clause() -> None:
    where, params = build_where(SuggestionFilter(languages=["es", "en"], limit=10, offset=0), "run-1")
    assert "language IN (?, ?)" in where
    assert params[-2:] == ["es", "en"]


def test_an_empty_language_list_matches_nothing() -> None:
    where, _params = build_where(SuggestionFilter(languages=[], limit=10, offset=0), "run-1")
    assert "1 = 0" in where


def test_suggestion_ids_filter_adds_an_id_in_clause() -> None:
    where, params = build_where(SuggestionFilter(suggestion_ids=[3, 4], limit=10, offset=0), "run-1")
    assert "id IN (?, ?)" in where
    assert params[-2:] == [3, 4]


def test_an_empty_suggestion_id_list_matches_nothing() -> None:
    where, _params = build_where(SuggestionFilter(suggestion_ids=[], limit=10, offset=0), "run-1")
    assert "1 = 0" in where


def test_no_constraints_means_the_run_and_hides_obsolete() -> None:
    where, params = build_where(SuggestionFilter(limit=10, offset=0), "run-1")
    assert where == "run_id = ? AND status != ?"
    assert params == ["run-1", "obsolete"]


def test_include_obsolete_drops_only_the_default_status_exclusion() -> None:
    where, params = build_where(SuggestionFilter(include_obsolete=True, limit=10, offset=0), "run-1")
    assert where == "run_id = ?"
    assert params == ["run-1"]


def test_an_explicit_obsolete_status_filter_shows_them() -> None:
    from uwazi_admin_agent.domain.suggestion import SuggestionStatus

    where, params = build_where(SuggestionFilter(status=SuggestionStatus.OBSOLETE), "run-1")
    assert where == "run_id = ? AND status = ?"
    assert params == ["run-1", "obsolete"]


def test_filter_from_controls_translates_the_language_select() -> None:
    assert filter_from_controls(language="es").languages == ["es"]
    assert filter_from_controls(language="").languages is None


def test_a_large_pair_selection_collapses_into_one_term_per_language() -> None:
    """A whole-result selection must not exceed SQLite's expression depth.

    A flat ``OR`` chain of one term per selected row is left-deep, so a run with
    a few thousand rows used to fail with "Expression tree is too large"
    (SQLITE_MAX_EXPR_DEPTH = 1000) before updating anything. Grouping the pairs
    by language yields ONE ``language IS NULL AND shared_id IN (...)`` term.
    """
    pairs = [(f"s{i}", None) for i in range(5007)]
    where, params = build_where(
        SuggestionFilter(entity_languages=pairs, include_obsolete=True),
        "run-1",
    )
    assert " OR " not in where
    assert where.count("?") == 5008
    assert _max_paren_depth(where) < 50
    assert params[1:] == [shared_id for shared_id, _language in pairs]


def test_pair_selection_groups_ids_per_language_and_dedupes() -> None:
    where, params = build_where(
        SuggestionFilter(
            entity_languages=[("a", None), ("b", None), ("a", None), ("c", "es")],
            include_obsolete=True,
        ),
        "run-1",
    )
    assert where.count(" OR ") == 1
    assert "language IS NULL AND shared_id IN (?, ?)" in where
    assert "language = ? AND shared_id IN (?)" in where
    assert params == ["run-1", "a", "b", "es", "c"]


def test_the_pair_selection_matches_exactly_the_selected_pairs() -> None:
    """Grouping preserves the semantics: only the selected pairs match."""
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE suggestion (id INTEGER PRIMARY KEY, run_id TEXT, shared_id TEXT, language TEXT)")
        conn.executemany(
            "INSERT INTO suggestion (run_id, shared_id, language) VALUES (?, ?, ?)",
            [
                ("run-1", "a", None),
                ("run-1", "a", "es"),
                ("run-1", "b", None),
                ("run-1", "b", "es"),
                ("run-1", "c", None),
                ("run-2", "a", "es"),
            ],
        )
        where, params = build_where(
            SuggestionFilter(
                entity_languages=[("a", "es"), ("b", None), ("c", None)],
                include_obsolete=True,
            ),
            "run-1",
        )
        rows = conn.execute(
            f"SELECT shared_id, language FROM suggestion WHERE {where} ORDER BY id",
            params,
        ).fetchall()
    finally:
        conn.close()
    assert rows == [("a", "es"), ("b", None), ("c", None)]
