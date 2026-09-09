"""The LLM's restatement of a prompt plus its clarifying questions.

Produced by :class:`ClarifyPromptUseCase` before generation so the operator can
confirm what the LLM understood and resolve ambiguities before the (expensive)
script-generation call runs.
"""

from pydantic import BaseModel, Field

from uwazi_admin_agent.domain.clarifying_question import ClarifyingQuestion


class PromptUnderstanding(BaseModel):
    """What the LLM understood from a prompt, plus the ambiguities it found."""

    summary: str = Field(description="The LLM's restatement of what it understood the prompt to mean.")
    questions: list[ClarifyingQuestion] = Field(
        default_factory=list,
        description="Clarifying questions for under-specified parts of the prompt (empty when fully specified).",
    )
