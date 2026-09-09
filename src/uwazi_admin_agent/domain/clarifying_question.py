"""A clarifying question the LLM asks to disambiguate an under-specified prompt.

Part of the prompt-validation step (§ prompt validation): before generation, a
lightweight LLM call restates the prompt and, for each ambiguous part, asks one
question with a handful of suggested answers. The operator ticks the suggestions
(or writes a custom answer) and the resolutions are folded into the final prompt.
"""

from pydantic import BaseModel, Field


class ClarifyingQuestion(BaseModel):
    """One question plus its suggested (checkbox) answers."""

    question: str = Field(description="The clarifying question.")
    options: list[str] = Field(
        default_factory=list,
        description="Suggested answers the operator can tick (the UI also offers a free-text custom answer).",
    )
