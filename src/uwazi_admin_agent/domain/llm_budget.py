"""Controlled LLM usage: price, budget, usage, ledger, and cost estimation (pure).

The admin agent's LLM calls split into two families:

- **generation** (the script-writing agent), capped by pydantic-ai's
  ``UsageLimits`` (``MAX_LLM_CALLS`` requests);
- **script LLM helpers** (``llm_complete`` / ``llm_map``, called by a generated
  script over entities), capped by an operator-approved :class:`LlmBudget`.

Both must be *controlled*: the operator sees an up-front ESTIMATE and approves
a HARD DOLLAR cap (``max_cost``). The cap — checked against the price-derived
cost of the actual/estimated token usage — is what actually aborts a runaway
pass; the request count is a secondary backstop against cheap-call loops.
Everything in this module is a
deterministic transform of its inputs, so it is the unit-test target for the
cost math and the ledger's abort semantics. The only mutable piece is
:class:`LlmLedger`, which is deliberately explicit about its single atomic
operation (``reserve`` then ``record``).

Money is a real enforced limit now: the operator's prices are placeholders
(configuration.py), so the dollar cap is enforced against the price-derived
cost estimate; the request ceiling is the hard backstop.
"""

from __future__ import annotations

import math
import threading
from typing import Iterable

from pydantic import BaseModel, ConfigDict, Field

# Characters-per-token heuristic (the industry rule of thumb for mixed English
# prose + markup). Documented as an ESTIMATE everywhere it surfaces: the cost
# dialog says "estimated", and the hard caps are what actually bound a pass.
CHARS_PER_TOKEN: int = 4


def estimate_tokens(text: str) -> int:
    """Estimate the token count of ``text`` (``ceil(len(text) / 4)``).

    Pure; an empty string estimates 0 tokens. Used to size a pass before it runs
    (sample one entity's document text, multiply by the scope size).
    """
    return math.ceil(len(text) / CHARS_PER_TOKEN)


class LlmPrice(BaseModel):
    """Which model a pass targets and what one million tokens of each kind costs."""

    model_config = ConfigDict(frozen=True)

    model: str = Field(description="The model identifier the price belongs to (e.g. 'deepseek-v4.1-flash:cloud').")
    input_per_mtok: float = Field(description="Cost in the operator's currency per 1M INPUT tokens.")
    output_per_mtok: float = Field(description="Cost in the operator's currency per 1M OUTPUT tokens.")

    def configured(self) -> bool:
        """True when a real price is known (either rate positive).

        A placeholder/unset price (0.0 on both rates) can't price usage, so a
        dollar budget cannot be projected or enforced against it — the caller
        (cost dialog) must refuse to start the pass with a dollar-only budget.
        """
        return self.input_per_mtok > 0 or self.output_per_mtok > 0


class LlmUsage(BaseModel):
    """Accumulated LLM consumption: requests plus input/output tokens."""

    model_config = ConfigDict(frozen=True)

    requests: int = Field(default=0, ge=0, description="Number of LLM requests issued.")
    input_tokens: int = Field(default=0, ge=0, description="Total input tokens consumed.")
    output_tokens: int = Field(default=0, ge=0, description="Total output tokens produced.")

    def add(self, other: LlmUsage) -> LlmUsage:
        """Return a new usage summing ``self`` and ``other`` (no mutation)."""
        return LlmUsage(
            requests=self.requests + other.requests,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )

    def total_tokens(self) -> int:
        """Input + output tokens (the number a single total-token cap compares against)."""
        return self.input_tokens + self.output_tokens


class LlmBudget(BaseModel):
    """An operator-approved ceiling for one LLM pass.

    Created only through the cost dialog's approval. The DOLLAR cap is the
    authoritative limit: it is checked (pre-call reservation, post-call
    accounting) against the price-derived cost of the usage; the request cap is
    a secondary backstop against runaway loops. ``price`` also feeds the
    estimate display.

    A cap may be 0 (the wizard's deny-by-default): ``max_requests=0`` aborts
    the pass on its first reserved call, and ``max_cost=0`` on the first call
    whose price-derived cost is positive — the ledger's clean
    :class:`LlmBudgetExceededError`, never a wasted request.

    ``max_cost`` is in the operator's currency (the same money the
    ``LlmPrice`` rates are denominated in).
    """

    model_config = ConfigDict(frozen=True)

    max_cost: float = Field(ge=0, description="Hard cap on the pass's estimated-money cost (0 = deny every call).")
    max_requests: int = Field(ge=0, description="Hard backstop on LLM requests for the pass (0 = deny every call).")
    price: LlmPrice = Field(description="The price used to convert usage into money.")

    def call_cost(self, in_tokens: int, out_tokens: int) -> float:
        """The cost of ``in_tokens``/``out_tokens`` under the budget's price (pure)."""
        return estimate_call_cost(self.price, in_tokens, out_tokens)


class LlmBudgetExceededError(RuntimeError):
    """Raised when an LLM call would breach the approved budget.

    The caller (the prediction pass) treats this as a clean abort: predictions
    never mutate Uwazi, so everything recorded before the failing call is kept
    and the attempt is marked ``ABORTED``.
    """

    def __init__(self, budget: LlmBudget, usage: LlmUsage, reason: str) -> None:
        self.budget = budget
        self.usage = usage
        self.reason = reason
        super().__init__(
            f"LLM budget exceeded ({reason}): used {usage.requests} request(s), "
            f"{format_money(budget.call_cost(usage.input_tokens, usage.output_tokens))} in cost "
            f"({usage.input_tokens} input + {usage.output_tokens} output tokens) against a cap of "
            f"{budget.max_requests} request(s) and {format_money(budget.max_cost)} in cost."
        )


def estimate_call_cost(price: LlmPrice, in_tokens: int, out_tokens: int) -> float:
    """Estimated cost of one call in the price's currency (pure)."""
    return (in_tokens / 1_000_000) * price.input_per_mtok + (out_tokens / 1_000_000) * price.output_per_mtok


def estimate_pass_cost(
    price: LlmPrice,
    n_calls: int,
    est_in_tokens_per_call: int,
    est_out_tokens_per_call: int,
) -> float:
    """Estimated cost of a whole pass of ``n_calls`` identical calls (pure)."""
    if n_calls <= 0:
        return 0.0
    return n_calls * estimate_call_cost(price, est_in_tokens_per_call, est_out_tokens_per_call)


def n_calls_for_entities(n_entities: int, batch_size: int) -> int:
    """Requests needed to cover ``n_entities`` entities at ``batch_size`` per call.

    The ``llm_map`` helper splits its input into batches of
    ``LLM_MAP_BATCH_SIZE``, so a pass over N entities costs ``ceil(N/batch)``
    requests (0 entities ⇒ 0 requests). Pure.
    """
    if n_entities <= 0 or batch_size <= 0:
        return 0
    return math.ceil(n_entities / batch_size)


def remaining(budget: LlmBudget, usage: LlmUsage) -> LlmUsage:
    """Head-room left in ``budget`` after ``usage`` (clamped at 0, pure).

    The money head-room is the dollar cap minus the price-derived cost of the
    usage so far; ``input_tokens`` carries that remaining amount scaled to
    token-equivalents (at the cheaper of the price's two rates) simply so the
    existing ``LlmUsage`` shape can express it. The request head-room is the
    usual request-cap difference.
    """
    spent_cost = budget.call_cost(usage.input_tokens, usage.output_tokens)
    money_left = max(budget.max_cost - spent_cost, 0.0)
    in_rate, out_rate = budget.price.input_per_mtok, budget.price.output_per_mtok
    rate = min((r for r in (in_rate, out_rate) if r > 0), default=0.0)
    tokens_left = int(money_left / rate * 1_000_000) if rate > 0 else 10**18
    return LlmUsage(
        requests=max(budget.max_requests - usage.requests, 0),
        input_tokens=tokens_left,
        output_tokens=0,
    )


def ensure_within_budget(budget: LlmBudget, usage: LlmUsage) -> None:
    """Raise :class:`LlmBudgetExceededError` when ``usage`` breaches ``budget``.

    Cost is the authoritative check: the price-derived money of the cumulative
    token usage is compared against ``max_cost``. The request count is a
    secondary backstop against a runaway loop whose calls are all cheap.
    Called both as a PRE-check (estimated sizes of the call about to be made)
    and a POST-check (real usage after the call). Pure decision; the message
    names the breached dimension so the operator sees why a pass stopped.
    """
    if budget.call_cost(usage.input_tokens, usage.output_tokens) > budget.max_cost:
        raise LlmBudgetExceededError(budget, usage, "cost")
    if usage.requests > budget.max_requests:
        raise LlmBudgetExceededError(budget, usage, "requests")


def format_money(value: float) -> str:
    """Render an estimated amount with 4 decimals (costs are per-mtok fractions)."""
    return f"{value:.4f}"


def format_usage(usage: LlmUsage) -> str:
    """One-line human summary of a usage snapshot (progress + abort messages)."""
    return f"{usage.requests} requests, {usage.input_tokens:,} input + {usage.output_tokens:,} output tokens"


def format_budget(budget: LlmBudget) -> str:
    """One-line human summary of an approved budget's enforced caps."""
    return (
        f"{format_money(budget.max_cost)} in cost, {budget.max_requests:,} request(s) hard backstop "
        f"(price: {budget.price.model})"
    )


class LlmLedger:
    """The single mutable piece of the budget machinery: a running account.

    The contract is two-phase so a runaway pass is stopped BEFORE the call that
    would breach the budget:

    1. :meth:`reserve` checks the budget against the current usage PLUS an
       estimated size for the call about to be made. It raises
       :class:`LlmBudgetExceededError` when that projection breaches the cap, so
       no call is issued past the ceiling.
    2. :meth:`record` folds the call's REAL usage in afterwards, which catches an
       underestimate (a pre-check can only be as good as its estimate).

    Only one worker thread drives a script's helpers at a time (the sandbox
    closures run on the script's thread), so a plain method pair is sufficient;
    the lock exists anyway because a progress reader may poll
    :meth:`snapshot` from the UI thread.
    """

    def __init__(self, budget: LlmBudget) -> None:
        self._budget: LlmBudget = budget
        self._usage: LlmUsage = LlmUsage()
        self._lock = threading.Lock()
        self._exceeded: bool = False

    @property
    def exceeded(self) -> bool:
        """True once any check has raised :class:`LlmBudgetExceededError`.

        The pass runner reads this instead of pattern-matching the error text:
        a tripped ledger is exactly "the budget stopped this pass", which is the
        expected, safe abort (unlike any other script error).
        """
        return self._exceeded

    @property
    def budget(self) -> LlmBudget:
        return self._budget

    def snapshot(self) -> LlmUsage:
        """Current accumulated usage (the progress/abort source of truth)."""
        with self._lock:
            return self._usage

    def reserve(self, est_in_tokens: int = 0, est_out_tokens: int = 0, requests: int = 1) -> None:
        """Pre-check the budget for a call about to be made; raise when it would breach.

        The projection is the current usage plus the call's estimated size, so a
        pass stops ONE CALL BEFORE exceeding the ceiling rather than after.
        """
        with self._lock:
            projected = self._usage.add(
                LlmUsage(requests=requests, input_tokens=est_in_tokens, output_tokens=est_out_tokens)
            )
        try:
            ensure_within_budget(self._budget, projected)
        except LlmBudgetExceededError:
            self._exceeded = True
            raise

    def record(self, usage: LlmUsage) -> LlmUsage:
        """Fold a call's real usage into the ledger; return the new snapshot.

        The post-check is what catches an under-estimate: if the real usage
        pushes past a cap the pass aborts here, after the call but before the
        next one.
        """
        with self._lock:
            self._usage = self._usage.add(usage)
            current = self._usage
        try:
            ensure_within_budget(self._budget, current)
        except LlmBudgetExceededError:
            self._exceeded = True
            raise
        return current


def sum_usage(items: Iterable[LlmUsage]) -> LlmUsage:
    """Sum an iterable of usages (empty ⇒ zero usage). Pure."""
    total = LlmUsage()
    for item in items:
        total = total.add(item)
    return total


__all__ = [
    "CHARS_PER_TOKEN",
    "LlmBudget",
    "LlmBudgetExceededError",
    "LlmLedger",
    "LlmPrice",
    "LlmUsage",
    "ensure_within_budget",
    "estimate_call_cost",
    "estimate_pass_cost",
    "estimate_tokens",
    "format_budget",
    "format_money",
    "format_usage",
    "n_calls_for_entities",
    "remaining",
    "sum_usage",
]
