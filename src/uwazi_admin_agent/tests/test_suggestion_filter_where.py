"""Isolated unit tests for the filter SQL builder and the control translation (pure)."""

from __future__ import annotations

from uwazi_admin_agent.adapters.sqlite_suggestion_store import build_where
from uwazi_admin_agent.domain.suggestion import SuggestionFilter
from uwazi_admin_agent.drivers.web.review_view import filter_from_controls


def test_language_filter_adds_an_in_clause() -> None:
    where, params = build_where(SuggestionFilter(languages=["es", "en"], limit=10, offset=0), "run-1", 1)
    assert "language IN (?, ?)" in where
    assert params[-2:] == ["es", "en"]


def test_an_empty_language_list_matches_nothing() -> None:
    where, _params = build_where(SuggestionFilter(languages=[], limit=10, offset=0), "run-1", 1)
    assert "1 = 0" in where


def test_suggestion_ids_filter_adds_an_id_in_clause() -> None:
    where, params = build_where(SuggestionFilter(suggestion_ids=[3, 4], limit=10, offset=0), "run-1", 1)
    assert "id IN (?, ?)" in where
    assert params[-2:] == [3, 4]


def test_an_empty_suggestion_id_list_matches_nothing() -> None:
    where, _params = build_where(SuggestionFilter(suggestion_ids=[], limit=10, offset=0), "run-1", 1)
    assert "1 = 0" in where


def test_no_constraints_means_the_run_and_attempt_and_hides_obsolete() -> None:
    where, params = build_where(SuggestionFilter(limit=10, offset=0), "run-1", 2)
    assert where == "run_id = ? AND attempt = ? AND status != ?"
    assert params == ["run-1", 2, "obsolete"]


def test_include_obsolete_drops_only_the_default_status_exclusion() -> None:
    where, params = build_where(SuggestionFilter(include_obsolete=True, limit=10, offset=0), "run-1", 2)
    assert where == "run_id = ? AND attempt = ?"
    assert params == ["run-1", 2]


def test_an_explicit_obsolete_status_filter_shows_them() -> None:
    from uwazi_admin_agent.domain.suggestion import SuggestionStatus

    where, params = build_where(SuggestionFilter(status=SuggestionStatus.OBSOLETE), "run-1", 2)
    assert where == "run_id = ? AND attempt = ? AND status = ?"
    assert params == ["run-1", 2, "obsolete"]


def test_filter_from_controls_translates_the_language_select() -> None:
    assert filter_from_controls(language="es").languages == ["es"]
    assert filter_from_controls(language="").languages is None
