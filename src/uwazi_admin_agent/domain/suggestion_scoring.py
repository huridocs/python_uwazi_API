"""Turn dry-run records into reviewable suggestions, and score them (pure).

Two responsibilities, both pure and both unit-tested in isolation:

- :func:`records_to_suggestions` — the conversion from the dry-run recorder's
  per-op dicts into :class:`Suggestion` rows. This is where the op vocabulary
  is normalized into a table the operator reviews: an ``update``/``create``
  record explodes into one row per metadata key (plus a ``_title`` row when the
  record carries a title), a whole-entity op yields exactly one row.
- :func:`compare_values` / :func:`score_accuracy` — accuracy against Uwazi's
  current values, so the operator can see WHERE the script's predictions fail
  before applying anything.

The conversion lives here rather than in the use case so it is testable from
literal record dicts with no ports, no I/O, and no running instance.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from uwazi_admin_agent.domain.metadata_diff import format_metadata_value
from uwazi_admin_agent.domain.suggestion import (
    TITLE_PROPERTY,
    Suggestion,
    SuggestionOp,
    SuggestionStatus,
)

# Whole-entity ops (no per-property breakdown): one suggestion per record.
_WHOLE_ENTITY_OPS: frozenset[str] = frozenset(
    {"delete", "publish", "unpublish", "set_publish_status", "create_relationships"}
)


def _as_op(raw: Any) -> SuggestionOp | None:
    """Coerce a record's ``op`` label into a known :data:`SuggestionOp` (None = unknown).

    ``move_files`` and ``delete_file`` records are deliberately NOT suggestions:
    they are file-collection operations the apply path does not push as entity
    property values (the dry-run report counts them for the step's implication
    preview instead).
    """
    value = str(raw or "")
    if value in ("update", "create", "delete", "publish", "unpublish", "set_publish_status", "create_relationships"):
        return value  # type: ignore[return-value] — the literal set above matches SuggestionOp exactly
    return None


def records_to_suggestions(
    run_id: str,
    attempt: int,
    records: list[dict[str, Any]],
    entity_titles: dict[str, str] | None = None,
    now: datetime | None = None,
) -> list[Suggestion]:
    """Convert dry-run records into suggestion rows (pure).

    Per-op expansion:

    - ``update``: one row per metadata key (``proposed_value`` = that key's
      value), plus one ``_title`` row when the record carries a non-None
      ``title``. Records with no metadata and no title produce a single row with
      ``property_name=None`` so the operator still sees the no-op touch.
    - ``create``: the same expansion, with the recorder's placeholder
      ``dry-created-N`` sharedId (creates have no real sharedId until applied).
    - ``delete`` / ``publish`` / ``unpublish`` / ``set_publish_status`` /
      ``create_relationships``: one row each, ``property_name=None``.

    ``entity_titles`` maps sharedId → title for display (the prediction use case
    resolves it from the entities it already read). Order follows the records so
    the review table's unsorted view mirrors the script's execution order. Rows
    carry the record's ``template_name`` when the script recorded one (the
    review table's Template column and the Run executor's write payloads).
    """
    titles = entity_titles or {}
    created = now or datetime.now(timezone.utc)
    suggestions: list[Suggestion] = []

    for index, record in enumerate(records):
        op = _as_op(record.get("op"))
        if op is None:
            continue
        shared_id = str(record.get("shared_id") or "")
        if op == "create" and not shared_id:
            shared_id = f"dry-created-{index}"
        title = titles.get(shared_id, "") or str(record.get("title") or "")
        base = {
            "run_id": run_id,
            "attempt": attempt,
            "shared_id": shared_id,
            "entity_title": title,
            "template_name": str(record.get("template_name") or "") or None,
            "op": op,
            "status": SuggestionStatus.PENDING,
            "created_at": created,
        }

        if op == "update" or op == "create":
            metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
            emitted = False
            if record.get("title") is not None:
                suggestions.append(Suggestion(**base, property_name=TITLE_PROPERTY, proposed_value=record.get("title")))
                emitted = True
            for key, value in metadata.items():
                suggestions.append(Suggestion(**base, property_name=str(key), proposed_value=value))
                emitted = True
            if not emitted:
                suggestions.append(Suggestion(**base, property_name=None, proposed_value=None))
            continue

        proposed: Any = None
        if op == "set_publish_status":
            proposed = bool(record.get("published"))
        suggestions.append(Suggestion(**base, property_name=None, proposed_value=proposed))

    return suggestions


def compare_values(proposed: Any, current: Any) -> bool | None:
    """Whether ``proposed`` matches ``current``, or None when there is nothing to compare.

    ``None`` current (missing key / absent entity value) means the suggestion
    ADDS a value — that is not a mismatch, so it scores as ``None`` ("new").
    Both sides are rendered through
    :func:`~uwazi_admin_agent.domain.metadata_diff.format_metadata_value` before
    comparison, so a list-vs-joined-text difference is never reported as a
    change (the same display-equality rule the results window uses). Pure.
    """
    if current is None:
        return None
    return format_metadata_value(proposed) == format_metadata_value(current)


class PropertyAccuracy(BaseModel):
    """Per-property accuracy counters (matched / mismatched / new)."""

    model_config = ConfigDict(frozen=True)

    property_name: str
    matched: int = 0
    mismatched: int = 0
    new: int = 0


class AccuracySummary(BaseModel):
    """Aggregate accuracy of a prediction set, overall plus per property."""

    model_config = ConfigDict(frozen=True)

    matched: int = 0
    mismatched: int = 0
    new: int = 0
    by_property: dict[str, PropertyAccuracy] = Field(default_factory=dict)

    @property
    def accuracy(self) -> float | None:
        """matched / (matched + mismatched), or None when nothing is comparable."""
        denominator = self.matched + self.mismatched
        if denominator <= 0:
            return None
        return self.matched / denominator


def score_accuracy(suggestions: list[Suggestion]) -> AccuracySummary:
    """Score a suggestion set against Uwazi's current values (pure).

    Only rows that carry a match verdict (property updates with a current value)
    move the counters; whole-entity ops and new values are counted separately.
    Property names are reported as-is (``_title`` for title changes), which is
    exactly the key the review UI's property filter uses.
    """
    by_property: dict[str, PropertyAccuracy] = {}
    matched = mismatched = new = 0

    for suggestion in suggestions:
        key = suggestion.property_name
        if key is None:
            continue
        stats = by_property.get(key) or PropertyAccuracy(property_name=key)
        if suggestion.match is True:
            matched += 1
            stats = stats.model_copy(update={"matched": stats.matched + 1})
        elif suggestion.match is False:
            mismatched += 1
            stats = stats.model_copy(update={"mismatched": stats.mismatched + 1})
        else:
            new += 1
            stats = stats.model_copy(update={"new": stats.new + 1})
        by_property[key] = stats

    return AccuracySummary(matched=matched, mismatched=mismatched, new=new, by_property=by_property)


__all__ = [
    "AccuracySummary",
    "PropertyAccuracy",
    "compare_values",
    "records_to_suggestions",
    "score_accuracy",
]
