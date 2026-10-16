"""Shared display helpers for the review surfaces (pure; no NiceGUI imports).

Both the review page and its entity window render the same values, so the
formatting rules live here once: how a proposed/current value is truncated, how
a match verdict is labelled, how a suggestion flattens into a table row, and how
the page's control values become a store filter.

Keeping this module free of NiceGUI imports is what makes it directly testable.
"""

from __future__ import annotations

from typing import Any

from uwazi_admin_agent.configuration import DUMMY_LANGUAGE
from uwazi_admin_agent.domain.metadata_diff import format_metadata_value
from uwazi_admin_agent.domain.suggestion import (
    EntityReviewRow,
    Suggestion,
    SuggestionFilter,
    SuggestionStatus,
    entity_language_key,
)

# Column/table op and status colors (the UI maps these onto Quasar badges).
STATUS_COLORS: dict[str, str] = {
    "pending": "grey",
    "accepted": "green",
    "edited": "teal",
    "discarded": "red",
    "applied": "blue",
    "reverted": "indigo",
}

OP_COLORS: dict[str, str] = {
    "update": "primary",
    "create": "secondary",
    "delete": "negative",
    "publish": "positive",
    "unpublish": "orange",
    "set_publish_status": "orange",
    "create_relationships": "purple",
}

MATCH_COLORS: dict[str, str] = {"exact": "green", "mismatch": "red", "new": "grey-6", "mixed": "orange", "n/a": "grey-4"}

# The entity table's Run column: has this entity's recorded work been written
# to Uwazi yet? Reuses the per-row statuses' colors ('applied' blue, 'pending'
# grey, 'reverted' indigo) so one state reads the same everywhere.
RUN_STATUS_COLORS: dict[str, str] = {"runned": "blue", "reverted": "indigo", "pending": "grey"}


# Ops whose entity ALREADY EXISTS in Uwazi, so the review row can link to its
# public page. A create row still holds its dry-created placeholder, so
# opening it would land on nothing.
OPENABLE_OPS: frozenset[str] = frozenset({"update", "delete"})

# How each recorded op reads in the Run dialog ("what changes in Uwazi?").
RUN_OP_PHRASES: dict[str, str] = {
    "update": "Update",
    "create": "Create",
    "delete": "Delete",
    "publish": "Publish",
    "unpublish": "Unpublish",
    "set_publish_status": "Change publishing",
    "create_relationships": "Relink",
}


def run_confirmation(count: int, ops: list[str] | None = None, languages: list[str] | None = None) -> tuple[str, str]:
    """``(title, message)`` for the Run dialog: a few words naming the change (pure).

    The dialog's job is consent, not explanation — it says what Run will do to
    Uwazi (``Delete 1 entity``) and nothing else. The ops come from the operator's
    own selection, so the wording is the change they just chose; unknown ops (or
    a Run triggered without them) fall back to the neutral ``Write``. The
    languages do the same for a translation task: "Run 2 rows (es)" says the
    Run writes the Spanish text and leaves the other languages alone.
    """
    entities = "row" if count == 1 else "rows"
    title = f"Run {count} {entities}"
    if languages:
        title = f"{title} ({', '.join(languages)})"
    phrases = [RUN_OP_PHRASES[op] for op in ops or [] if op in RUN_OP_PHRASES]
    action = " + ".join(dict.fromkeys(phrases)) or "Write"
    return title, f"{action} {count} {entities} in Uwazi now. Revertable from the task page."


def run_button_label(count: int) -> str:
    """The Run button's text for a live selection of ``count`` rows (pure).

    The button carries the selection count, so the operator sees what Run will
    act on without opening the dialog.
    """
    return f"Run ({count})"


def prune_selection(selected: list[Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only the selected rows that are STILL on the page (pure).

    Reloading the table replaces the rows without clearing the checkbox
    selection, so after a filter change or a page flip the hidden rows would
    still be counted in ``Run (N)`` — and Run would act on entities the
    operator can no longer see. Rows are matched on ``row_key`` (the table's
    entity+language identity, so a translation's two rows are two distinct
    selections) and the selection's own order is kept.
    """
    visible = {str(row.get("row_key")) for row in rows}
    return [row for row in selected if isinstance(row, dict) and str(row.get("row_key")) in visible]


def truncate(value: Any, limit: int = 80) -> str:
    """Render any value as one display line, truncated with an ellipsis (pure)."""
    text = format_metadata_value(value)
    return text if len(text) <= limit else text[:limit] + "…"


def match_label(match: bool | None) -> str:
    """The operator-facing verdict label for a match computation (pure)."""
    if match is True:
        return "exact"
    if match is False:
        return "mismatch"
    return "new"


def suggestion_to_row(suggestion: Suggestion) -> dict[str, Any]:
    """Flatten a suggestion into the change table's row contract (pure).

    Colors are resolved HERE (Python) rather than looked up in the Vue slot: Vue
    templates can only reach a whitelist of globals, so a ``window.X`` lookup in a
    slot silently renders empty. Carrying the color on the row keeps the slot a
    plain property read.
    """
    label = match_label(suggestion.match)
    return {
        "id": suggestion.id,
        "shared_id": suggestion.shared_id,
        "title": suggestion.entity_title or suggestion.shared_id,
        "op": suggestion.op,
        "op_color": OP_COLORS.get(suggestion.op, "grey"),
        "property": suggestion.property_name or "—",
        "proposed": truncate(suggestion.proposed_value),
        "current": truncate(suggestion.current_value),
        "match": label,
        "match_color": MATCH_COLORS.get(label, "grey"),
        "status": suggestion.status.value,
        "status_color": STATUS_COLORS.get(suggestion.status.value, "grey"),
        "editable": suggestion.op in ("update", "create"),
    }


def filter_from_controls(
    *,
    status: str = "",
    op: str = "",
    property_name: str = "",
    match: str = "",
    text: str = "",
    offset: int = 0,
    limit: int = 25,
    accepted_only: bool = False,
    shared_ids: list[str] | None = None,
) -> SuggestionFilter:
    """Build the server-side filter from the page's raw control values (pure).

    Empty strings mean "no constraint" — the UI's select controls use "" for
    their "all" option, and translating that here keeps the page's handlers
    trivial. ``shared_ids`` is the table's checkbox selection passed through
    verbatim (``None`` = no constraint, ``[]`` = match nothing).
    """
    return SuggestionFilter(
        status=SuggestionStatus(status) if status else None,
        op=op or None,
        property_name=property_name or None,
        match=match or None,  # type: ignore[arg-type] — validated by the pydantic Literal
        text=(text or "").strip() or None,
        accepted_only=accepted_only,
        shared_ids=shared_ids,
        limit=limit,
        offset=offset,
    )


def run_status(item: EntityReviewRow) -> tuple[str, str]:
    """The entity's Run state as ``(label, color)`` — the table's Status column (pure).

    ``runned`` once EVERY recorded row of the entity is APPLIED (written to
    Uwazi); ``reverted`` once every row is REVERTED — a revert undid the writes,
    so the operator can run the entity again, row by row or whole. Anything else
    (a row still to review, a mix of applied and reverted) is ``pending``: work
    remains.
    """
    if item.applied >= item.proposal_count:
        label = "runned"
    elif item.reverted >= item.proposal_count:
        label = "reverted"
    else:
        label = "pending"
    return label, RUN_STATUS_COLORS[label]


def entity_open_url(item: EntityReviewRow, uwazi_url: str | None) -> str | None:
    """The instance's public page for this entity, or None when it has none.

    Uwazi serves an entity at ``/<language>/entity/<sharedId>``, the same URL
    the results window links from — and the ``<language>`` segment is the ROW's
    language, so a translated row opens the entity as that language. A
    language-independent row (``None``) has no locale to ask for, so it opens the
    default one. None — and so no icon — when ``UWAZI_URL`` is not configured, or
    when the row's ops leave nothing in Uwazi to open yet (a create row still
    holds its dry-created placeholder, and publishing/relinking are not about
    the entity's current content).
    """
    if not uwazi_url:
        return None
    if not OPENABLE_OPS.intersection(item.ops):
        return None
    return f"{uwazi_url.rstrip('/')}/{item.language or DUMMY_LANGUAGE}/entity/{item.shared_id}"


def entity_row(item: EntityReviewRow, uwazi_url: str | None = None) -> dict[str, Any]:
    """The entity table's row contract (pure). Colors resolved here, not in Vue.

    One row per affected entity IN ONE LANGUAGE (the table is the
    checkbox-selection surface the bulk Run action reads), aggregated from the
    entity's suggestion rows by the store. Columns: Entity (title), Language,
    Template, Ops, Status (runned/pending), the Run/Edit row actions, and the
    Open-in-Uwazi link — no sharedId, no change-count columns.
    ``uwazi_url`` is the controlled instance's base URL (None = unconfigured, so
    the row carries no link). ``row_key`` is the entity+language identity, so the
    same entity in two languages is two distinct checkable rows.
    """
    status_label, status_color = run_status(item)
    return {
        "row_key": entity_language_key(item.shared_id, item.language),
        "shared_id": item.shared_id,
        "title": item.entity_title or item.shared_id,
        "language": item.language or "",
        "template": ", ".join(item.template_names) or "—",
        "ops": ", ".join(item.ops),
        "op_colors": [OP_COLORS.get(op, "grey") for op in item.ops],  # used by the slot
        "url": entity_open_url(item, uwazi_url),  # used by the Open column's slot
        "run_status": status_label,
        "run_status_color": status_color,  # used by the slot
    }


def accuracy_label(exact: int, mismatch: int) -> str:
    """The accuracy figure shown in the header, or 'n/a' when nothing compares (pure)."""
    denominator = exact + mismatch
    return f"{100 * exact / denominator:.1f}%" if denominator else "n/a"


__all__ = [
    "MATCH_COLORS",
    "OPENABLE_OPS",
    "OP_COLORS",
    "RUN_OP_PHRASES",
    "RUN_STATUS_COLORS",
    "STATUS_COLORS",
    "accuracy_label",
    "entity_row",
    "entity_open_url",
    "entity_language_key",
    "filter_from_controls",
    "match_label",
    "prune_selection",
    "run_button_label",
    "run_confirmation",
    "run_status",
    "suggestion_to_row",
    "truncate",
]
