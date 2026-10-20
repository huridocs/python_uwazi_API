"""Pure before/after rendering of an :class:`AppliedWrite` (§ results window).

The results window shows, per touched entity, which properties a run changed.
The raw material is one :class:`~uwazi_admin_agent.domain.manifest.AppliedWrite`
— recorded by the backup intercept from the exact payload the generated script
sent, paired with the before-values of only the keys that payload touched. This
module turns that pair into ordered :class:`PropertyChange` rows. No I/O, no
ports, no Uwazi access: the diff is computable from the manifest alone.

Both sides are formatted through :func:`format_metadata_value` before comparison,
so a change that is only a rendering artifact (a list vs. its joined text) never
shows as a change — ``changed`` compares the two DISPLAY strings, which is
exactly what the operator sees.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from uwazi_admin_agent.domain.manifest import AppliedWrite

# Display truncation, matching ``_format_value`` in the web driver.
MAX_VALUE_CHARS: int = 200


class PropertyChange(BaseModel):
    """One property's before/after, as shown in the changes table."""

    model_config = ConfigDict(frozen=True)

    property: str = Field(description="The property name (or 'title' / 'published').")
    before: str = Field(description="The formatted before-value; '—' when there was none.")
    after: str = Field(description="The formatted after-value; '—' when it was cleared.")
    changed: bool = Field(description="Whether the two formatted values differ.")


def render_metadata_value(value: Any) -> str:
    """Render one metadata value as display text, WITHOUT truncation (pure).

    Same shape handling as :func:`format_metadata_value` (Uwazi ``{"value",
    "label"}`` list entries, other lists/tuples, booleans, scalars) but keeps
    the whole value. Newlines inside a text/richtext value are preserved, so a
    caller rendering it with CSS ``white-space: pre-wrap`` shows the complete
    paragraph instead of one clipped line.
    """
    return _render(value)


def format_metadata_value(value: Any) -> str:
    """Render one metadata value (either shape) as a single display line.

    Handles the Uwazi raw shape (a list of ``{"value": ..., "label"?: ...}``
    dicts — the label is shown when present, since a thesaurus label is what the
    operator reads), other lists/tuples, booleans (lowercase), and scalars.
    ``None`` renders as an em dash. Long output is truncated to
    :data:`MAX_VALUE_CHARS` with a trailing ellipsis, matching the touch-set
    table's formatting. Use :func:`render_metadata_value` where the FULL value
    must be shown (the review entity window).
    """
    text = render_metadata_value(value)
    return text if len(text) <= MAX_VALUE_CHARS else text[:MAX_VALUE_CHARS] + "…"


def _render(value: Any) -> str:
    """The untruncated rendering of one metadata value."""
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ", ".join(_render_item(item) for item in value)
    if isinstance(value, dict):
        return _render_item(value)
    return str(value)


def _render_item(item: Any) -> str:
    """Render one element of a list value (a Uwazi ``{"value": …}`` dict, or a scalar)."""
    if isinstance(item, dict):
        if item.get("label") is not None:
            return str(item["label"])
        if "value" in item:
            return _render(item["value"])
        return str(item)
    return _render(item)


def build_property_changes(write: AppliedWrite) -> list[PropertyChange]:
    """Build the ordered property changes one applied write produced.

    Order: the ``title`` entry first (when the write carried a title), then the
    metadata keys in the payload's insertion order, or a single ``published``
    entry for publish/unpublish ops (which carry no metadata payload).

    A key present only in the applied payload is a set/new property: its before
    renders as ``"—"`` (the empty ``metadata_before`` of a create, or a property
    the script added). ``changed`` compares the two formatted strings.
    """
    if write.op in ("publish", "unpublish"):
        before = format_metadata_value(write.metadata_before.get("published"))
        after = "true" if write.op == "publish" else "false"
        return [PropertyChange(property="published", before=before, after=after, changed=before != after)]

    changes: list[PropertyChange] = []
    if write.title is not None:
        before = format_metadata_value(write.title_before)
        after = format_metadata_value(write.title)
        changes.append(PropertyChange(property="title", before=before, after=after, changed=before != after))
    for key, after_value in write.metadata.items():
        before_text = format_metadata_value(write.metadata_before.get(key))
        after_text = format_metadata_value(after_value)
        changes.append(PropertyChange(property=key, before=before_text, after=after_text, changed=before_text != after_text))
    return changes


__all__ = [
    "MAX_VALUE_CHARS",
    "PropertyChange",
    "build_property_changes",
    "format_metadata_value",
    "render_metadata_value",
]
