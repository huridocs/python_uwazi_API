"""Isolated unit tests for the plan gate (pure, per AGENTS.md).

The gate decides whether a recorded plan is a review TABLE or a failed
generation. It is judged on the ROWS the table would show, not on the records a
script emitted — that distinction is the whole bug it fixes: 343 recorded
updates whose values Uwazi already held rendered as one info row.
"""

from uwazi_admin_agent.domain.plan_intent import plan_outcome, plan_refusal
from uwazi_admin_agent.domain.suggestion import Suggestion

INFO = {"op": "info", "shared_id": "info-0", "title": "0 entities updated"}


def _row(op: str = "update", **kw) -> Suggestion:
    return Suggestion(run_id="r", shared_id=kw.pop("shared_id", "s1"), op=op, **kw)


# --- records only (not hydrated yet) ---------------------------------------


def test_a_changes_plan_with_one_update_is_accepted() -> None:
    assert plan_refusal([{"op": "update", "shared_id": "s1"}], None, "changes") is None


def test_a_changes_plan_with_only_info_rows_is_refused() -> None:
    """The reported failure: a change request that gave up became ONE info row."""
    problem = plan_refusal([INFO], None, "changes", "0 of 40 entities matched")

    assert problem is not None
    assert "info row" in problem
    assert "0 entities updated" in problem
    assert "0 of 40 entities matched" in problem


def test_a_changes_plan_with_no_records_is_refused() -> None:
    problem = plan_refusal([], None, "changes")
    assert problem is not None
    assert "no operation at all" in problem


def test_a_changes_plan_of_file_deletes_names_the_missing_review_row() -> None:
    """A file-only plan has no review row today; say so instead of "gave up"."""
    problem = plan_refusal([{"op": "delete_file", "shared_id": "s1"}], None, "changes")

    assert problem is not None
    assert "FILE deletions" in problem


# --- rendered rows (the gate's real input) ---------------------------------


def test_hundreds_of_already_applied_updates_are_not_a_failed_generation() -> None:
    """THE REPORTED RUN: 343 recorded updates, every value already stored.

    Hydration drops them all, leaving the script's skip report as the only row.
    That is a plan with nothing to do — the entities are up to date — so it must
    be ACCEPTED and explained, not refused as a gave-up generation.
    """
    records = [{"op": "update", "shared_id": f"s{i}"} for i in range(343)]
    records.append({"op": "info", "shared_id": "info-0", "title": "105 entities could not be processed"})
    rendered = [_row(op="info", shared_id="info-0", proposed_value="105 entities could not be processed")]

    assert plan_refusal(records, rendered, "changes", "recorded 343 updates") is None

    outcome = plan_outcome(records, rendered)
    assert outcome.change_rows == 0
    assert outcome.unchanged_rows == 343
    assert outcome.only_dropped_no_ops is True


def test_a_plan_that_recorded_nothing_and_rendered_nothing_is_refused() -> None:
    assert plan_refusal([INFO], [], "changes") is not None


def test_an_all_skipped_change_plan_is_legitimate_entity_evidence() -> None:
    """Every entity was inspected and skipped for a concrete reason.

    The table now carries one skipped row per entity, so this is a completed
    outcome — not the "script gave up with one info row" case the gate refuses.
    """
    records = [
        {"op": "update", "shared_id": "s1"},
        {"op": "skipped", "shared_id": "s1", "reason": "no_segmentation"},
        {"op": "skipped", "shared_id": "s2", "reason": "no_code_in_page_1"},
    ]
    rendered = [
        _row(op="skipped", shared_id="s1", skip_reason="no_segmentation"),
        _row(op="skipped", shared_id="s2", skip_reason="no_code_in_page_1"),
    ]

    assert plan_refusal(records, rendered, "changes") is None
    outcome = plan_outcome(records, rendered)
    assert outcome.change_rows == 0
    assert outcome.skipped_rows == 2
    assert outcome.has_runnable_work is False


def test_the_skipped_rows_beside_real_changes_are_not_counted_as_the_plan() -> None:
    records = [
        {"op": "update", "shared_id": "s1"},
        {"op": "info", "shared_id": "info-0", "title": "2 failed"},
    ]
    rendered = [_row(), _row(op="info", shared_id="info-0", proposed_value="2 failed")]

    assert plan_refusal(records, rendered, "changes") is None
    assert plan_outcome(records, rendered).change_rows == 1


# --- plan kinds ------------------------------------------------------------


def test_a_query_plan_of_info_rows_is_accepted() -> None:
    assert plan_refusal([INFO], None, "query") is None


def test_a_query_plan_that_records_writes_is_refused() -> None:
    problem = plan_refusal([{"op": "update", "shared_id": "s1"}], None, "query")

    assert problem is not None
    assert "plan_kind='query'" in problem


def test_a_whole_entity_op_counts_as_a_change() -> None:
    assert plan_refusal([{"op": "delete", "shared_id": "s1"}], None, "changes") is None


def test_an_undeclared_legacy_plan_kind_is_not_judged() -> None:
    """Runs generated before the declaration existed keep working."""
    assert plan_refusal([INFO], None, None) is None
