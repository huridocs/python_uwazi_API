"""Isolated unit tests for the script-budget prompt section (per AGENTS.md).

Pure rendering only: no LLM, no store, no network.
"""

from uwazi_admin_agent.domain.llm_budget import LlmBudget, LlmPrice
from uwazi_admin_agent.use_cases.script_budget import (
    render_script_budget_section,
    script_budget_enabled,
    system_prompt_with_script_budget,
)


def _budget(cost: float = 5.0, requests: int = 20) -> LlmBudget:
    return LlmBudget(
        max_cost=cost,
        max_requests=requests,
        price=LlmPrice(model="test-model", input_per_mtok=0.14, output_per_mtok=0.55),
    )


def test_no_budget_leaves_the_prompt_untouched() -> None:
    assert render_script_budget_section(None) == ""
    assert system_prompt_with_script_budget("BASE", None) == "BASE"
    assert script_budget_enabled(None) is False


def test_a_positive_dollar_and_request_budget_enables_the_llm_helpers() -> None:
    budget = _budget()
    assert script_budget_enabled(budget) is True
    section = render_script_budget_section(budget)
    assert "SCRIPT LLM BUDGET" in section
    assert "20 request(s)" in section
    assert "$5.00" in section
    assert "llm_map" in section


def test_the_deny_by_default_budget_disables_the_llm_helpers() -> None:
    budget = _budget(cost=0.0, requests=0)
    assert script_budget_enabled(budget) is False
    section = render_script_budget_section(budget)
    assert "DISABLED" in section
    assert "pure Python" in section


def test_a_zero_on_either_cap_matches_the_ledger_and_disables_calls() -> None:
    # The ledger checks both caps: a zero request cap or a zero priced dollar
    # cap aborts the first call. The prompt must not promise an allowance that
    # the planning pass would refuse.
    assert script_budget_enabled(_budget(cost=5.0, requests=0)) is False
    assert script_budget_enabled(_budget(cost=0.0, requests=20)) is False


def test_an_unpriced_zero_dollar_cap_cannot_deny_calls() -> None:
    # With no configured price the cost of any call is 0, so only the request
    # cap is enforced; a positive request cap is a real allowance.
    unpriced = LlmBudget(
        max_cost=0.0,
        max_requests=3,
        price=LlmPrice(model="test-model", input_per_mtok=0.0, output_per_mtok=0.0),
    )
    assert script_budget_enabled(unpriced) is True


def test_the_section_is_appended_after_the_base_prompt() -> None:
    prompt = system_prompt_with_script_budget("BASE CONTRACT", _budget())
    assert prompt.startswith("BASE CONTRACT")
    assert prompt.rstrip().endswith("within them.")
