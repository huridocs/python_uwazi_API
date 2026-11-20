"""Render the operator-approved script budget into the script-generation prompt.

Two different budgets exist and must not be confused:

- the **generation** conversation (the agent writing the script), capped by
  ``MAX_LLM_CALLS`` / ``LLM_MAX_OUTPUT_TOKENS``;
- the **generated script's** own ``llm_complete``/``llm_map`` calls, capped by
  the operator's budget from the wizard's caps step.

Only the second one is operator-editable per task. The generator must know it:
with no allowance it has to stay pure-Python; with an allowance it should use
the LLM where real model judgment is worth the spend instead of stretching a
fragile heuristic. The rendering is pure, so it is unit-tested directly.
"""

from __future__ import annotations

from uwazi_admin_agent.domain.llm_budget import LlmBudget


def script_budget_enabled(budget: LlmBudget | None) -> bool:
    """Whether the script's LLM helpers may run under ``budget`` (pure).

    Mirrors the ledger: both caps are checked, so a zero request cap or a zero
    dollar cap (with a configured price) denies the first call. ``None`` means
    no operator budget was supplied (the CLI path), so no section is added.
    """
    if budget is None:
        return False
    return budget.max_requests > 0 and (budget.max_cost > 0 or not budget.price.configured())


def render_script_budget_section(budget: LlmBudget | None) -> str:
    """The ``SCRIPT LLM BUDGET`` prompt block for one approved budget (pure)."""
    if budget is None:
        return ""
    requests = f"{budget.max_requests:,} request(s)"
    money = f"${budget.max_cost:,.2f}"
    if not script_budget_enabled(budget):
        return (
            "SCRIPT LLM BUDGET: DISABLED.\n"
            f"The approved caps for the generated script's `llm_complete`/`llm_map` calls are "
            f"{requests} and {money}. A 0 on either cap aborts the first call and stops the "
            "planning pass, so do NOT call the LLM helpers: compute every value in pure Python."
        )
    return (
        "SCRIPT LLM BUDGET (for the generated script — approved at task creation)\n"
        f"The operator approved at most {requests} and {money} for the script's own "
        "`llm_complete`/`llm_map` calls. This allowance applies ONLY to those calls, never to "
        "your generation conversation.\n"
        "Use the LLM where genuine model judgment improves the result and pure Python cannot "
        "compute the value reliably — free-text extraction, a classification, or a summary that "
        "needs understanding — and batch with `llm_map` so many entities fit in few requests. "
        "Prefer pure Python where it is deterministic and sufficient, and never call the LLM "
        "speculatively or to re-derive a value the discovery reads already carry. The caps are "
        "hard: a call beyond them aborts the planning pass, so design the script to finish "
        "within them."
    )


def system_prompt_with_script_budget(base_prompt: str, budget: LlmBudget | None) -> str:
    """Append the budget contract to a generation system prompt (pure)."""
    section = render_script_budget_section(budget)
    if not section:
        return base_prompt
    return f"{base_prompt.rstrip()}\n\n{section}\n"


__all__ = [
    "render_script_budget_section",
    "script_budget_enabled",
    "system_prompt_with_script_budget",
]
