"""Unit tests for :func:`allowance_note_to_user_block` (pure, offline)."""

from uwazi_admin_agent.domain.allowance_note import allowance_note_to_user_block
from uwazi_admin_agent.domain.llm_budget import LlmBudget, LlmPrice


def _budget(max_cost: float) -> LlmBudget:
    return LlmBudget(
        max_cost=max_cost,
        max_requests=1,
        price=LlmPrice(model="m", input_per_mtok=1.0, output_per_mtok=2.0),
    )


def test_allowance_note_empty_when_budget_unset() -> None:
    """No allowance approved → the sent prompt is unchanged."""
    assert allowance_note_to_user_block(None) == ""
    assert allowance_note_to_user_block(object()) == ""


def test_allowance_note_wraps_positive_cost_in_a_delimited_block() -> None:
    note = allowance_note_to_user_block(_budget(1.5))
    assert note.startswith("\n\n--- OPERATOR-SET AI ALLOWANCE ---\n")
    assert note.endswith("\n--- END AI ALLOWANCE ---")
    assert "$1.50" in note
    assert "stopping when reached" in note


def test_allowance_note_zero_cost_is_the_deny_by_default_wording() -> None:
    note = allowance_note_to_user_block(_budget(0.0))
    assert "may NEVER" in note
    assert "computable" in note
