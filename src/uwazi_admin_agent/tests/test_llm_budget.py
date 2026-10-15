"""Isolated unit tests for the LLM budget math + ledger (per AGENTS.md).

Pure: literal inputs, plain assertions, no mocks, no I/O. The ledger is
exercised as a real object (it is the only stateful piece and its abort
semantics are the point of the feature).
"""

import pytest

from uwazi_admin_agent.domain.llm_budget import (
    CHARS_PER_TOKEN,
    LlmBudget,
    LlmBudgetExceededError,
    LlmLedger,
    LlmPrice,
    LlmUsage,
    ensure_within_budget,
    estimate_call_cost,
    estimate_pass_cost,
    estimate_tokens,
    format_budget,
    format_money,
    format_usage,
    n_calls_for_entities,
    remaining,
    sum_usage,
)


def _price(input_rate: float = 0.14, output_rate: float = 0.55) -> LlmPrice:
    return LlmPrice(model="deepseek-v4.1-flash:cloud", input_per_mtok=input_rate, output_per_mtok=output_rate)


def _budget(cost: float = 10.0, requests: int = 100) -> LlmBudget:
    return LlmBudget(max_cost=cost, max_requests=requests, price=_price())


# --- token heuristic ---------------------------------------------------------


def test_estimate_tokens_is_ceil_over_four() -> None:
    assert CHARS_PER_TOKEN == 4
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2
    assert estimate_tokens("a" * 4000) == 1000


# --- cost math ---------------------------------------------------------------


def test_estimate_call_cost_mixes_both_rates() -> None:
    # 1M input @0.14 + 1M output @0.55 = 0.69
    assert estimate_call_cost(_price(), 1_000_000, 1_000_000) == pytest.approx(0.69)
    assert estimate_call_cost(_price(), 0, 0) == 0.0


def test_estimate_pass_cost_multiplies_by_call_count() -> None:
    assert estimate_pass_cost(_price(), 10, 500, 100) == pytest.approx(10 * estimate_call_cost(_price(), 500, 100))
    assert estimate_pass_cost(_price(), 0, 500, 100) == 0.0
    assert estimate_pass_cost(_price(), -3, 500, 100) == 0.0


def test_n_calls_for_entities_batches_up() -> None:
    assert n_calls_for_entities(0, 10) == 0
    assert n_calls_for_entities(1, 10) == 1
    assert n_calls_for_entities(10, 10) == 1
    assert n_calls_for_entities(11, 10) == 2
    assert n_calls_for_entities(9500, 10) == 950
    assert n_calls_for_entities(5, 0) == 0


def test_price_configured_only_when_a_rate_is_positive() -> None:
    assert _price().configured() is True
    assert LlmPrice(model="m", input_per_mtok=0.0, output_per_mtok=0.55).configured() is True
    assert LlmPrice(model="m", input_per_mtok=0.0, output_per_mtok=0.0).configured() is False


# --- budget model ------------------------------------------------------------


def test_budget_call_cost_uses_its_price() -> None:
    budget = _budget()
    assert budget.call_cost(1_000_000, 1_000_000) == pytest.approx(0.69)


def test_budget_rejects_nonpositive_cost_or_requests() -> None:
    with pytest.raises(ValueError):
        LlmBudget(max_cost=0, max_requests=5, price=_price())
    with pytest.raises(ValueError):
        LlmBudget(max_cost=10, max_requests=0, price=_price())


# --- usage arithmetic --------------------------------------------------------


def test_usage_add_and_total_tokens() -> None:
    a = LlmUsage(requests=2, input_tokens=10, output_tokens=3)
    b = LlmUsage(requests=1, input_tokens=5, output_tokens=7)
    total = a.add(b)
    assert (total.requests, total.input_tokens, total.output_tokens) == (3, 15, 10)
    assert total.total_tokens() == 25
    # add returns a new object: the sources are untouched.
    assert (a.requests, a.input_tokens, a.output_tokens) == (2, 10, 3)


def test_sum_usage_over_empty_is_zero() -> None:
    assert sum_usage([]) == LlmUsage()
    assert sum_usage([LlmUsage(requests=1), LlmUsage(requests=2, input_tokens=4)]).requests == 3


def test_remaining_reports_money_and_request_headroom() -> None:
    usage = LlmUsage(requests=7, input_tokens=1_000_000, output_tokens=1_000_000)
    left = remaining(_budget(cost=1.0, requests=5), usage)
    # Spent 0.69, so 0.31 left in money; at the cheaper rate (0.14/1M) that is
    # floor(0.31/0.14 * 1M) = 2,214,285 token-equivalents.
    assert left.requests == 0
    assert left.input_tokens == 2_214_285


def test_remaining_clamps_money_at_zero() -> None:
    usage = LlmUsage(requests=7, input_tokens=10_000_000, output_tokens=10_000_000)
    left = remaining(_budget(cost=1.0, requests=5), usage)
    assert left.requests == 0
    assert left.input_tokens == 0


# --- budget enforcement ------------------------------------------------------


def test_ensure_within_budget_allows_usage_just_under_the_cost_cap() -> None:
    # 1M+1M tokens cost 0.69; a hair above the cap still passes.
    budget = _budget(cost=0.7, requests=5)
    usage = LlmUsage(requests=2, input_tokens=1_000_000, output_tokens=1_000_000)
    ensure_within_budget(budget, usage)


def test_ensure_within_budget_raises_on_cost_breach_naming_cost() -> None:
    with pytest.raises(LlmBudgetExceededError) as excinfo:
        ensure_within_budget(
            _budget(cost=0.5, requests=5),
            LlmUsage(requests=2, input_tokens=1_000_000, output_tokens=1_000_000),  # costs 0.69 > 0.5
        )
    assert excinfo.value.reason == "cost"
    assert "0.5000" in str(excinfo.value)


def test_ensure_within_budget_raises_on_request_backstop_naming_requests() -> None:
    with pytest.raises(LlmBudgetExceededError) as excinfo:
        ensure_within_budget(_budget(cost=100.0, requests=5), LlmUsage(requests=6))
    assert excinfo.value.reason == "requests"
    assert "requests" in str(excinfo.value)


def test_request_backstop_stops_a_cheap_call_loop() -> None:
    # Many tiny calls each well under the dollar cap still stop at the request cap.
    budget = _budget(cost=1_000.0, requests=3)
    with pytest.raises(LlmBudgetExceededError) as excinfo:
        ensure_within_budget(budget, LlmUsage(requests=4, input_tokens=400, output_tokens=400))
    assert excinfo.value.reason == "requests"


def test_budget_error_message_names_money_and_tokens() -> None:
    budget = _budget(cost=1.0, requests=5)
    with pytest.raises(LlmBudgetExceededError) as excinfo:
        ensure_within_budget(budget, LlmUsage(requests=1, input_tokens=4_000_000, output_tokens=2_000_000))
    message = str(excinfo.value)
    assert "1.6600" in message  # 4M@0.14 + 2M@0.55 = 0.56 + 1.10
    assert "1.0000" in message  # the cap


# --- ledger ------------------------------------------------------------------


def test_ledger_reserve_blocks_the_call_that_would_exceed() -> None:
    ledger = LlmLedger(_budget(cost=1.0, requests=3))
    # Three reservations fit...
    for _ in range(3):
        ledger.reserve(est_in_tokens=100, est_out_tokens=100)
        ledger.record(LlmUsage(requests=1, input_tokens=100, output_tokens=100))
    assert ledger.snapshot().requests == 3
    # ...the next REFUSES on the request backstop BEFORE the call.
    with pytest.raises(LlmBudgetExceededError) as excinfo:
        ledger.reserve(est_in_tokens=100, est_out_tokens=100)
    assert excinfo.value.reason == "requests"
    assert ledger.snapshot().requests == 3


def test_ledger_reserve_blocks_on_cost_when_money_runs_out_first() -> None:
    # 1M+1M tokens cost 0.69 each round (price 0.14/0.55): two rounds = 1.38 > 1.0.
    ledger = LlmLedger(_budget(cost=1.0, requests=100))
    ledger.reserve(est_in_tokens=1_000_000, est_out_tokens=1_000_000)
    ledger.record(LlmUsage(requests=1, input_tokens=1_000_000, output_tokens=1_000_000))
    with pytest.raises(LlmBudgetExceededError) as excinfo:
        ledger.reserve(est_in_tokens=1_000_000, est_out_tokens=1_000_000)
    assert excinfo.value.reason == "cost"
    assert ledger.snapshot().requests == 1


def test_ledger_record_catches_an_underestimated_call() -> None:
    ledger = LlmLedger(_budget(cost=0.5, requests=10))
    ledger.reserve(est_in_tokens=1, est_out_tokens=1)  # estimate says it fits
    with pytest.raises(LlmBudgetExceededError) as excinfo:
        ledger.record(LlmUsage(requests=1, input_tokens=5_000_000, output_tokens=500_000))
    assert excinfo.value.reason == "cost"  # 0.975 > 0.5
    # The real usage is still folded in: the abort message reports truth.
    assert ledger.snapshot().input_tokens == 5_000_000


def test_ledger_snapshot_starts_empty_and_budget_is_exposed() -> None:
    budget = _budget(cost=7.0, requests=7)
    ledger = LlmLedger(budget)
    assert ledger.snapshot() == LlmUsage()
    assert ledger.budget is budget


# --- formatting --------------------------------------------------------------


def test_format_money_always_four_decimals() -> None:
    assert format_money(0.0) == "0.0000"
    assert format_money(1.5) == "1.5000"
    assert format_money(0.00012) == "0.0001"


def test_format_usage_is_readable() -> None:
    assert (
        format_usage(LlmUsage(requests=12, input_tokens=1000, output_tokens=2000))
        == "12 requests, 1,000 input + 2,000 output tokens"
    )


def test_format_budget_names_cost_cap_and_requests() -> None:
    text = format_budget(_budget(cost=10.0, requests=2000))
    assert "10.0000" in text
    assert "2,000" in text
