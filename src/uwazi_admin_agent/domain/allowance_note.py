"""The AI-allowance note folded into the prompt-validation LLM call.

The wizard's caps step runs BEFORE validation: the operator's approved dollar
limit is appended to the clarify prompt so the LLM tailors its restatement and
clarifying questions to what the task can actually afford. :func:`allowance_note`
is the pure seam — unit-tested without mocks.
"""

from __future__ import annotations

from typing import Any


def allowance_note_to_user_block(budget: Any) -> str:
    """The operator-approved AI allowance, phrased for the clarification agent.

    Appended to the USER prompt (not the system prompt): the allowance is the
    operator's per-run decision, not part of the standing contract. ``budget``
    is an :class:`~uwazi_admin_agent.domain.llm_budget.LlmBudget` (or anything
    exposing ``max_cost``). Returns ``""`` for an unset allowance so the sent
    text is unchanged; a set allowance becomes a clearly-delimited block.
    """
    cost = getattr(budget, "max_cost", None)
    if cost is None:
        return ""
    body = _allowance_body(cost)
    return f"\n\n--- OPERATOR-SET AI ALLOWANCE ---\n{body}\n--- END AI ALLOWANCE ---"


def _allowance_body(cost: float) -> str:
    if cost <= 0:
        return (
            "AI ALLOWANCE (set by the operator): 0 — the generated script may NEVER "
            "call the AI after construction. Any proposed change has to be computable "
            "from the data itself (formulas, lookups, string operations). Do not ask "
            "questions that would steer the task toward AI-derived values (summaries, "
            "classifications, translations of document text); if the prompt seems to "
            "require them, say in your restatement that the current AI allowance "
            "cannot cover that and what lowering it to data-only logic would look like."
        )
    return (
        f"AI ALLOWANCE (set by the operator): at most ${cost:.2f} for the generated "
        "script's own AI calls after construction, stopping when reached. Keep this "
        "budget in mind when you restate the prompt and ask questions: if the request "
        "looks like it would need the AI per item (summaries, reading documents, "
        "classifying), you may ask a clarifying question about the scope that keeps "
        "the task within the allowance (e.g. fewer entities, or a data-only rule "
        "instead). Do not ask questions about the allowance itself."
    )
