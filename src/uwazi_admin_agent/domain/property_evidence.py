"""The entity's OWN property values as evidence for a suggestion (pure).

A script that infers a value from the entity's existing data — its title, its
``document_ref`` — records the property names it read under ``basis``. The
review surfaces must show those values: they are the input text the operator
needs to check the proposal, exactly as a ``context`` passage is the input text
for a value read from a document.

Everything here is pure: a raw Uwazi row in, display text out.
"""

from __future__ import annotations

from typing import Any

from uwazi_admin_agent.domain.metadata_diff import format_metadata_value

# The synthetic name a script uses to point at the entity's title (see
# ``uwazi_admin_agent.domain.suggestion.TITLE_PROPERTY``). Kept local so this
# module stays a leaf with a single import.
TITLE_PROPERTY: str = "_title"


def basis_values(raw: dict[str, Any], names: list[str]) -> dict[str, Any]:
    """The live value of each inferred-from property (pure; ``_title`` reads the title).

    ``raw`` is one entity's raw Uwazi row: ``_title`` comes from its top-level
    ``title``, every other name from its ``metadata`` cell (the WHOLE cell — a
    shared property has one cell, so no language narrowing applies). A name the
    row does not carry maps to ``None``, which the caller renders as an empty
    value instead of failing.
    """
    metadata = raw.get("metadata") or {}
    values: dict[str, Any] = {}
    for name in names:
        if name == TITLE_PROPERTY:
            values[name] = raw.get("title")
        else:
            values[name] = metadata.get(name) if isinstance(metadata, dict) else None
    return values


def basis_context_text(
    raw: dict[str, Any] | None,
    names: list[str],
    labels: dict[str, str] | None = None,
) -> str:
    """One row's evidence text: each basis property with its value (pure).

    ``""`` when there is no basis, no raw, or no readable value — the row then
    shows whatever ``context`` it recorded (or nothing). A readable value is
    prefixed with the property's LABEL when the template resolved one, so the
    operator reads "Document Ref: A/HRC/37/2", not ``document_ref: …``.
    """
    if raw is None or not names:
        return ""
    resolved = labels or {}
    values = basis_values(raw, names)
    lines: list[str] = []
    for name in names:
        text = format_metadata_value(values.get(name)).strip()
        if not text or text == "—":
            continue
        heading = resolved.get(name) or ("Title" if name == TITLE_PROPERTY else name)
        lines.append(f"{heading}: {text}")
    return "\n".join(lines)


__all__ = ["TITLE_PROPERTY", "basis_context_text", "basis_values"]
