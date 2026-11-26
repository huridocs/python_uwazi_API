"""The test-set note folded into the prompt-understanding LLM call (pure).

The wizard asks the operator to define the held-out test set BEFORE the LLM
restates the task. This note carries that decision into the call, so the
clarifying questions can be about what the score measures — how the property is
determined, what counts as correct, how to treat missing values — instead of
only about the prompt's wording. :func:`validation_note_to_user_block` is the
pure seam, unit-tested without a model.
"""

from __future__ import annotations

from typing import Any

_MEASURED_MODES = frozenset({"automatic", "custom"})


def validation_note_to_user_block(config: Any) -> str:
    """Describe the operator's test set for the clarification agent (pure).

    ``config`` is a :class:`~uwazi_admin_agent.domain.validation.ValidationConfig`
    (or anything exposing ``mode``/``property_name``/``property_type``/
    ``sample_size``/``selector_explanation``/``template_names``). Returns ``""``
    when no test set will be kept, so the sent text is unchanged.
    """
    mode = str(getattr(config, "mode", "") or "")
    property_name = str(getattr(config, "property_name", "") or "").strip()
    if mode not in _MEASURED_MODES or not property_name:
        return ""
    body = _body(config, mode, property_name)
    return f"\n\n--- OPERATOR-CHOSEN TEST SET ---\n{body}\n--- END TEST SET ---"


def _body(config: Any, mode: str, property_name: str) -> str:
    """The note's lines: what is measured, what is sampled, what to ask."""
    lines = [
        "The score on this set is how the operator will judge the finished task.",
        "It is a HELD-OUT set: the task must never read, change or learn from these entities.",
        f"- Property the score measures: {_property(property_name, config)}",
        f"- Sample: {_sample(mode, config)}",
        f"- Drawn from: {_templates(config)}",
        "",
        "Ask questions that make the score meaningful (how the measured value should be determined, what "
        "counts as correct, how to handle a missing value). Never propose reading or editing the held-out "
        "entities, and do not ask about the test set itself.",
    ]
    return "\n".join(lines)


def _property(property_name: str, config: Any) -> str:
    property_type = str(getattr(config, "property_type", "") or "").strip()
    return f"{property_name} ({property_type})" if property_type else property_name


def _sample(mode: str, config: Any) -> str:
    size = getattr(config, "sample_size", None) or 0
    try:
        size = max(1, int(size))
    except (TypeError, ValueError):
        size = 1
    if mode == "custom":
        explanation = str(getattr(config, "selector_explanation", "") or "").strip()
        if explanation:
            return f"up to {size} entities chosen by this rule: {explanation}"
        return f"up to {size} entities chosen by a custom rule"
    return f"up to {size} entities, picked automatically (the population may be smaller)"


def _templates(config: Any) -> str:
    names = [str(name).strip() for name in getattr(config, "template_names", ()) or () if str(name).strip()]
    return ", ".join(names) if names else "the templates the task targets"


__all__ = ["validation_note_to_user_block"]
