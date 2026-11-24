"""Isolated unit tests for the review-table refresh rules (per AGENTS.md).

Pure functions only: status merge decisions, the preflight comparison, and the
restore-value lookup. No store, no network, no mocks.
"""

from uwazi_admin_agent.domain.review_refresh import (
    ReconciliationSummary,
    evaluate_preflight,
    keeps_after_refresh,
    matched_swap_pairs,
    reconciled_status,
    restored_value,
)
from uwazi_admin_agent.domain.suggestion import Suggestion, SuggestionStatus


def _suggestion(**kwargs: object) -> Suggestion:
    base: dict[str, object] = {
        "id": 1,
        "run_id": "r",
        "shared_id": "entity-1",
        "op": "update",
        "property_name": "excerpt",
        "language": "en",
        "proposed_value": [{"value": "new"}],
        "current_value": [{"value": "old"}],
        "match": False,
        "status": SuggestionStatus.PENDING,
    }
    base.update(kwargs)
    return Suggestion(**base)


def _raw(value: object, *, language: str = "en", title: str = "Title") -> dict[str, object]:
    return {
        "sharedId": "entity-1",
        "language": language,
        "title": title,
        "metadata": {"excerpt": [{"value": value}]},
    }


# --- status merge ------------------------------------------------------------


def test_unchanged_proposal_keeps_the_operator_status() -> None:
    assert reconciled_status(SuggestionStatus.ACCEPTED, same=True) is SuggestionStatus.ACCEPTED
    assert reconciled_status(SuggestionStatus.APPLIED, same=True) is SuggestionStatus.APPLIED


def test_changed_proposal_resets_a_reviewable_row_to_pending() -> None:
    assert reconciled_status(SuggestionStatus.ACCEPTED, same=False) is SuggestionStatus.PENDING
    assert reconciled_status(SuggestionStatus.APPLIED, same=False) is SuggestionStatus.PENDING


def test_changed_proposal_keeps_deliberate_operator_decisions() -> None:
    assert reconciled_status(SuggestionStatus.EDITED, same=False) is SuggestionStatus.EDITED
    assert reconciled_status(SuggestionStatus.DISCARDED, same=False) is SuggestionStatus.DISCARDED


def test_a_refreshed_reverted_recreating_row_becomes_runnable_again() -> None:
    """A delete/merge undo re-creates entities; a live refresh resolves them."""
    assert reconciled_status(SuggestionStatus.REVERTED, same=True, rerun_after_refresh=True) is SuggestionStatus.PENDING
    # Without the flag the reverted row keeps its state (e.g. an ordinary
    # reverted update, which never needed the extra block).
    assert reconciled_status(SuggestionStatus.REVERTED, same=True) is SuggestionStatus.REVERTED
    # A discard is an operator decision and is never undone by a refresh.
    assert reconciled_status(SuggestionStatus.DISCARDED, same=True, rerun_after_refresh=True) is SuggestionStatus.DISCARDED


def test_missing_applied_row_is_kept_as_history() -> None:
    assert keeps_after_refresh(SuggestionStatus.APPLIED) is True


def test_missing_non_applied_row_is_deleted() -> None:
    """A stale row that never got written is deleted, not parked as a placeholder.

    A retired row still counts in the header cards and the Op/Status filters, so
    leaving it behind made one entity read as two rows (the earlier pass's
    ``skipped`` row under a language the entity no longer writes, plus the new
    proposal).
    """
    assert keeps_after_refresh(SuggestionStatus.PENDING) is False
    assert keeps_after_refresh(SuggestionStatus.ACCEPTED) is False
    assert keeps_after_refresh(SuggestionStatus.EDITED) is False
    assert keeps_after_refresh(SuggestionStatus.DISCARDED) is False
    assert keeps_after_refresh(SuggestionStatus.REVERTED) is False


def test_reconciliation_summary_mentions_the_operator_facing_counts() -> None:
    summary = ReconciliationSummary(inserted=2, refreshed=1, status_reset=3, removed=4, kept_applied=1)
    text = summary.summary_text()
    assert "2 new" in text
    assert "1 refreshed" in text
    assert "3 reset" in text
    assert "4 removed" in text
    assert "1 applied kept" in text


# --- preflight ---------------------------------------------------------------


def test_preflight_accepts_a_row_whose_live_value_is_unchanged() -> None:
    row = _suggestion()
    result = evaluate_preflight([row], {("entity-1", "en"): _raw("old")}, "en")
    assert result.ok


def test_preflight_blocks_a_row_whose_live_value_changed() -> None:
    row = _suggestion()
    result = evaluate_preflight([row], {("entity-1", "en"): _raw("someone else")}, "en")
    assert not result.ok
    assert result.issues[0].kind == "changed"
    assert "Sync with Uwazi" in result.message()


def test_preflight_blocks_a_row_whose_entity_is_gone() -> None:
    row = _suggestion(op="delete", property_name=None)
    result = evaluate_preflight([row], {}, "en")
    assert not result.ok
    assert result.issues[0].kind == "missing"


def test_preflight_skips_creates_and_non_runnable_rows() -> None:
    create = _suggestion(shared_id="dry-created-1", op="create")
    applied = _suggestion(status=SuggestionStatus.APPLIED, current_value=[{"value": "different"}])
    result = evaluate_preflight([create, applied], {}, "en")
    assert result.ok


def test_preflight_compares_only_the_rows_own_language() -> None:
    row = _suggestion(language="es", current_value=[{"value": "hola"}])
    # Uwazi answered with the English row: the Spanish value is still "not present".
    result = evaluate_preflight([row], {("entity-1", "es"): _raw("hello", language="en")}, "en")
    assert not result.ok


def test_preflight_accepts_an_absent_language_when_the_row_recorded_none() -> None:
    row = _suggestion(language="es", current_value=None)
    result = evaluate_preflight([row], {("entity-1", "es"): _raw("hello", language="en")}, "en")
    assert result.ok


# --- restore values ----------------------------------------------------------


def test_a_matched_row_is_paired_with_the_update_that_replaces_it() -> None:
    """The row keeps its identity slot instead of being deleted + a duplicate."""
    matched = ("entity-1", "matched", "en", "excerpt", "excerpt")
    update = ("entity-1", "update", "en", "excerpt", "")
    pairs = matched_swap_pairs([(matched, "matched", "en", "excerpt")], [(update, "update", "en", "excerpt")])
    assert pairs == [(matched, update)]


def test_a_property_that_now_matches_is_paired_with_its_stored_update() -> None:
    update = ("entity-1", "update", "en", "excerpt", "")
    matched = ("entity-1", "matched", "en", "excerpt", "excerpt")
    pairs = matched_swap_pairs([(update, "update", "en", "excerpt")], [(matched, "matched", "en", "excerpt")])
    assert pairs == [(update, matched)]


def test_no_swap_when_the_table_already_holds_both_rows_for_one_property() -> None:
    """A stored matched row + its update must not collide on promotion."""
    matched = ("entity-1", "matched", "en", "excerpt", "excerpt")
    update = ("entity-1", "update", "en", "excerpt", "")
    pairs = matched_swap_pairs(
        [(matched, "matched", "en", "excerpt"), (update, "update", "en", "excerpt")],
        [(update, "update", "en", "excerpt")],
    )
    assert pairs == []


def test_rows_of_another_language_or_property_are_not_paired() -> None:
    matched = ("entity-1", "matched", "es", "excerpt", "excerpt")
    update = ("entity-1", "update", "en", "excerpt", "")
    assert matched_swap_pairs([(matched, "matched", "es", "excerpt")], [(update, "update", "en", "excerpt")]) == []


def test_restored_value_reads_a_property_from_the_snapshot() -> None:
    snapshot = _raw("before")
    assert restored_value(snapshot, "excerpt") == [{"value": "before"}]


def test_restored_value_reads_the_title() -> None:
    assert restored_value({"title": "Old title"}, "_title") == "Old title"
