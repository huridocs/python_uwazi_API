"""Isolated unit tests for the cost-estimate model behind the approval dialog.

The dialog itself needs a NiceGUI client, but everything it *computes* — the
projected call count, the money estimate, the enforced dollar cap, and the
rows it prints — is pure, so the numbers the operator approves are pinned here.
"""

from uwazi_admin_agent.domain.llm_budget import LlmPrice, estimate_pass_cost
from uwazi_admin_agent.drivers.web.cost_dialog import (
    SpendEstimate,
    build_budget,
    estimate_summary,
)


def _estimate(**kwargs: object) -> SpendEstimate:
    base: dict[str, object] = {
        "title": "Generate predictions",
        "n_calls": 10,
        "n_entities": 100,
        "est_in_tokens_per_call": 1000,
        "est_out_tokens_per_call": 100,
    }
    base.update(kwargs)
    return SpendEstimate(**base)  # type: ignore[arg-type]


def test_estimate_cost_matches_the_pure_pass_cost() -> None:
    estimate = _estimate()
    expected = estimate_pass_cost(estimate.price(), 10, 1000, 100)
    assert estimate.est_cost() == expected


def test_estimate_cost_is_zero_without_calls() -> None:
    assert _estimate(n_calls=0).est_cost() == 0.0


def test_price_reflects_the_configured_rates() -> None:
    from uwazi_admin_agent.configuration import LLM_PRICE_INPUT_PER_MTOK, LLM_PRICE_OUTPUT_PER_MTOK

    price: LlmPrice = _estimate().price()
    assert price.input_per_mtok == LLM_PRICE_INPUT_PER_MTOK
    assert price.output_per_mtok == LLM_PRICE_OUTPUT_PER_MTOK


def test_build_budget_uses_the_defaults_when_inputs_are_missing() -> None:
    budget = build_budget(_estimate(default_max_cost=5.5, default_max_requests=55))
    assert (budget.max_cost, budget.max_requests) == (5.5, 55)


def test_build_budget_honors_tightened_caps() -> None:
    budget = build_budget(_estimate(), max_cost=0.25, max_requests=3)
    assert (budget.max_cost, budget.max_requests) == (0.25, 3)


def test_build_budget_falls_back_on_nonpositive_or_invalid_caps() -> None:
    for bad in (0, -5, None):
        budget = build_budget(
            _estimate(default_max_cost=7.5, default_max_requests=9),
            max_cost=bad,  # type: ignore[arg-type]
            max_requests=bad,  # type: ignore[arg-type]
        )
        assert (budget.max_cost, budget.max_requests) == (7.5, 9)


def test_budget_price_matches_the_estimate() -> None:
    estimate = _estimate()
    assert build_budget(estimate).price == estimate.price()


def test_estimate_summary_names_the_entity_and_call_counts() -> None:
    summary = estimate_summary(_estimate(n_entities=2500, n_calls=250))
    assert "2,500 entities" in summary
    assert "250 projected requests" in summary
    assert "estimated cost" in summary


def test_estimate_carries_an_entity_scope_through() -> None:
    assert _estimate(entity_scope=["a", "b"]).entity_scope == ["a", "b"]
    assert _estimate().entity_scope is None
