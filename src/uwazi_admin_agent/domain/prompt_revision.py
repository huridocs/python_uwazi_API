"""Turning a WRONG suggestion into a prompt-revision request (pure).

The review table shows what the run's script WOULD write. When a proposed value
is wrong, the fix belongs in the SCRIPT's logic, not in one row: the entity
detail window attaches the offending row (the entity, the property, Uwazi's
current value and the value the script proposed) to the prompt chat, so the
operator can say why it is wrong and the generation LLM revises the script with
that evidence in hand.

``correction_request`` is the text that request opens with; the operator's
explanation is folded into it. ``prompt_with_previous_script`` is how the fix
flow sends the run's CURRENT script alongside that prompt, so the generator
REVISES a script the operator already reviewed instead of recreating it from
scratch.

The helpers here are PURE (no I/O, no LLM) so the exact text the generation LLM
receives is unit-testable.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

# The heading the correction's own details are rendered under, in the request
# the generation LLM receives.
CORRECTION_HEADING: str = "## Incorrect suggestion to fix"

# The heading the run's CURRENT script is attached under. The fix flow sends it
# with the prompt: the generator must revise that script, not start over.
PREVIOUS_SCRIPT_HEADING: str = "## Current script (revise it, do not start over)"

# What the trailing instruction asks for: a GENERAL rule, never a one-row hack.
_CORRECTION_INSTRUCTION: str = (
    "Change the SCRIPT's logic so it stops producing this value. Fix the general rule the "
    "script follows — do NOT special-case this entity's sharedId — and keep every other "
    "value the script already produces. Use the run's current script as the starting point "
    "and change only what this correction requires."
)


class SuggestionCorrection(BaseModel):
    """One review row the operator flagged as wrong, plus their explanation.

    Built from a :class:`~uwazi_admin_agent.domain.suggestion.Suggestion` by the
    entity window (the values are already rendered for display) and handed to
    the prompt chat, which records it as the opening request and renders it as
    an attachment above the conversation.
    """

    model_config = ConfigDict(frozen=True)

    shared_id: str
    entity_title: str = ""
    template_name: str | None = None
    language: str | None = None
    property_name: str | None = None
    property_label: str | None = None
    current_value: str = ""
    proposed_value: str = ""
    evidence: str = ""
    reason: str = ""


def property_line(correction: SuggestionCorrection) -> str:
    """How the flagged property is named in the request ("Document Ref (document_ref)")."""
    label = (correction.property_label or "").strip()
    name = (correction.property_name or "").strip()
    if label and name and label != name:
        return f"{label} ({name})"
    return label or name or "(whole entity)"


def correction_summary(correction: SuggestionCorrection) -> str:
    """The compact attachment the chat shows above its conversation (pure)."""
    title = (correction.entity_title or "").strip() or correction.shared_id
    lines = [f"{title} — {correction.shared_id}"]
    if correction.template_name:
        lines.append(f"Template: {correction.template_name}")
    property_text = property_line(correction)
    if correction.language:
        property_text = f"{property_text} · {correction.language}"
    lines.append(f"Property: {property_text}")
    lines.append(f"Current value in Uwazi: {correction.current_value or '— (empty)'}")
    lines.append(f"Proposed value (wrong): {correction.proposed_value or '— (empty)'}")
    return "\n".join(lines)


def correction_request(correction: SuggestionCorrection) -> str:
    """The operator request that opens the fix turn (pure).

    It carries everything the generation LLM needs to understand the defect —
    the entity, the template/property, Uwazi's current value, the value the
    script proposed — plus the operator's explanation when they gave one. The
    closing instruction asks for a general rule, because a correction that only
    patches one entity leaves the rest of the table broken.
    """
    title = (correction.entity_title or "").strip() or correction.shared_id
    reason = correction.reason.strip() or "(the operator did not explain it; infer the cause from this row)"
    sections: list[str] = [
        CORRECTION_HEADING,
        f"Entity: {title} ({correction.shared_id})",
        f"Template: {correction.template_name or '(unknown)'}",
        f"Language: {correction.language or '(not language-specific)'}",
        f"Property: {property_line(correction)}",
        "",
        "Current value in Uwazi:",
        correction.current_value or "— (empty)",
        "",
        "Value the current script proposed (incorrect):",
        correction.proposed_value or "— (empty)",
    ]
    evidence = correction.evidence.strip()
    if evidence:
        sections.extend(["", "Evidence the script recorded for this row:", evidence])
    sections.extend(["", "Why this suggestion is wrong:", reason, "", _CORRECTION_INSTRUCTION])
    return "\n".join(sections)


def prompt_with_previous_script(prompt: str, script: str) -> str:
    """Send ``prompt`` alongside the run's CURRENT script (pure).

    The fix flow's generator must revise a known-good script instead of writing
    a new one: the review table was built by that script, so keeping the parts
    that already work is what makes a correction a correction. An empty script
    (a run with no committed source) leaves the prompt untouched.
    """
    prompt_text = (prompt or "").strip()
    script_text = (script or "").strip()
    if not script_text:
        return prompt_text
    return (
        f"{prompt_text}\n\n"
        f"{PREVIOUS_SCRIPT_HEADING}\n"
        "This is the run's current, committed script — the one whose review table the "
        "operator is fixing. Revise it: apply the correction above and keep everything "
        "else that already works.\n\n"
        f"```python\n{script_text}\n```"
    )


__all__ = [
    "CORRECTION_HEADING",
    "PREVIOUS_SCRIPT_HEADING",
    "SuggestionCorrection",
    "correction_request",
    "correction_summary",
    "property_line",
    "prompt_with_previous_script",
]
