"""The assembled, validated prompt that is actually sent to the generation LLM.

The prompt-validation step collects the operator's resolutions of the LLM's
clarifying questions (ticked suggestions and/or free-text custom answers) plus any
additional notes, and folds them into the original prompt. :func:`build_final_prompt`
is the pure seam that assembles that final prompt — unit-tested without mocks.
"""

from pydantic import BaseModel, Field


class QuestionAnswer(BaseModel):
    """The operator's resolution of one clarifying question."""

    question: str = Field(description="The question being answered.")
    selected: list[str] = Field(default_factory=list, description="The ticked suggested options.")
    custom: str | None = Field(default=None, description="Free-text answer, if the operator wrote one.")


def build_final_prompt(original_prompt: str, answers: list[QuestionAnswer], notes: str) -> str:
    """Assemble the final prompt from the original prompt + resolved answers + notes.

    Pure: no I/O. The final prompt is what the generation LLM receives; it carries
    the original request plus the operator's resolutions so the LLM does not have
    to re-guess the ambiguities. Questions with no selection and no custom answer
    are omitted (the operator left them unresolved).
    """
    parts = [original_prompt.strip()]
    resolved = [a for a in answers if a.selected or (a.custom and a.custom.strip())]
    if resolved:
        lines = ["", "Clarifications (resolved during validation):"]
        for answer in resolved:
            lines.append(f"- {answer.question}")
            for option in answer.selected:
                lines.append(f"  - {option}")
            if answer.custom and answer.custom.strip():
                lines.append(f"  - {answer.custom.strip()}")
        parts.append("\n".join(lines))
    if notes.strip():
        parts.append(f"\nAdditional notes:\n{notes.strip()}")
    return "\n".join(parts)
