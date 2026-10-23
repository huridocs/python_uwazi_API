"""Isolated unit tests for the review-table refresh rules (per AGENTS.md).

Pure functions only: status merge decisions, the preflight comparison, and the
restore-value lookup. No store, no network, no mocks.
"""

from uwazi_admin_agent.domain.review_refresh import (
    ReconciliationSummary,
    evaluate_preflight,
    missing_status,
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


def test_returning_obsolete_row_becomes_pending() -> None:
    assert reconciled_status(SuggestionStatus.OBSOLETE, same=True) is SuggestionStatus.PENDING


def test_missing_applied_row_is_kept_as_history() -> None:
    assert missing_status(SuggestionStatus.APPLIED) is SuggestionStatus.APPLIED


def test_missing_non_applied_row_is_obsolete() -> None:
    assert missing_status(SuggestionStatus.PENDING) is SuggestionStatus.OBSOLETE
    assert missing_status(SuggestionStatus.REVERTED) is SuggestionStatus.OBSOLETE


def test_reconciliation_summary_mentions_the_operator_facing_counts() -> None:
    summary = ReconciliationSummary(inserted=2, refreshed=1, status_reset=3, obsolete=4, kept_applied=1)
    text = summary.summary_text()
    assert "2 new" in text
    assert "1 refreshed" in text
    assert "3 reset" in text
    assert "4 obsolete" in text
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
    assert "Update table from Uwazi" in result.message()


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


def test_restored_value_reads_a_property_from_the_snapshot() -> None:
    snapshot = _raw("before")
    assert restored_value(snapshot, "excerpt") == [{"value": "before"}]


def test_restored_value_reads_the_title() -> None:
    assert restored_value({"title": "Old title"}, "_title") == "Old title"
