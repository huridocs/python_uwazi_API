"""Shared display helpers for the review surfaces (pure; no NiceGUI imports).

Both the review page and its entity window render the same values, so the
formatting rules live here once: how a proposed/current value is truncated, how
a match verdict is labelled, how a suggestion flattens into a table row, how a
property is named for a human (its label, with the raw name as a caption), and
how the page's control values become a store filter. The table's column widths
live here too, next to the row contract they belong to.

Keeping this module free of NiceGUI imports is what makes it directly testable.
"""

from __future__ import annotations

from typing import Any

from uwazi_admin_agent.configuration import DUMMY_LANGUAGE
from uwazi_admin_agent.domain.metadata_diff import format_metadata_value
from uwazi_admin_agent.domain.suggestion import (
    PROPERTY_OPS,
    TITLE_PROPERTY,
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
    "update_and_delete": "deep-purple",
    "create_and_delete": "indigo",
}

MATCH_COLORS: dict[str, str] = {"exact": "green", "mismatch": "red", "new": "grey-6", "mixed": "orange", "n/a": "grey-4"}

# The entity table's Run column: has this entity's recorded work been written
# to Uwazi yet? Reuses the per-row statuses' colors ('applied' blue, 'pending'
# grey, 'reverted' indigo) so one state reads the same everywhere — plus the
# 'discarded' red, the state an operation the operator refused to execute ends
# up in.
RUN_STATUS_COLORS: dict[str, str] = {
    "runned": "blue",
    "reverted": "indigo",
    "pending": "grey",
    "discarded": "red",
}


# Ops whose entity ALREADY EXISTS in Uwazi, so the review row can link to its
# public page. A create row still holds its dry-created placeholder, so
# opening it would land on nothing.
OPENABLE_OPS: frozenset[str] = frozenset({"update", "delete", "update_and_delete"})

# ``_title`` is the review table's synthetic name for a title change — no
# template property carries it, so it gets the same human word Uwazi uses.
TITLE_LABELS: dict[str, str] = {TITLE_PROPERTY: "Title"}

# How each recorded op reads in the Run dialog ("what changes in Uwazi?").
RUN_OP_PHRASES: dict[str, str] = {
    "update": "Update",
    "create": "Create",
    "delete": "Delete",
    "publish": "Publish",
    "unpublish": "Unpublish",
    "set_publish_status": "Change publishing",
    "create_relationships": "Relink",
    "update_and_delete": "Merge (update + delete)",
    "create_and_delete": "Merge (create + delete)",
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


def revert_entity_confirmation(shared_id: str, tooltip: str = "") -> tuple[str, str]:
    """``(title, message)`` for the per-row Revert dialog (pure).

    The restore rolls the WHOLE entity back — every language row the backup
    captured — because the rows share one backup per entity; the message says
    so (and shows the backup's creation time when one exists) so reverting the
    ``es`` row never silently rewrites the ``en`` text.
    """
    when = f" made {tooltip.removeprefix('entity backup creation ')}" if tooltip else ""
    return (
        "Revert entity",
        f"Restore {shared_id} to its backup{when}. ALL languages of this entity are restored (they share one backup).",
    )


def run_button_label(count: int) -> str:
    """The Run button's text for a live selection of ``count`` rows (pure).

    The button carries the selection count, so the operator sees what Run will
    act on without opening the dialog.
    """
    return f"Run ({count})"


def bulk_revert_label(count: int) -> str:
    """The Revert button's text for a live selection of ``count`` entities (pure).

    Mirrors :func:`run_button_label`: the button carries the count so the
    operator sees what Revert acts on without opening the dialog.
    """
    return f"Revert ({count})"


def bulk_discard_label(count: int) -> str:
    """The Discard button's text for a live selection of ``count`` rows (pure).

    The same shape as :func:`run_button_label`: the count on the button says
    how many checked operations the Discard will mark as not-to-be-executed.
    """
    return f"Discard ({count})"


def discard_confirmation(count: int) -> tuple[str, str]:
    """``(title, message)`` for the bulk Discard dialog (pure).

    The dialog is consent, not explanation: it says how many checked operations
    will be marked as discarded and that a Run will not execute them. The reason
    itself is asked in the same dialog (it is stored with the rows), so the
    operator can say WHY the end user does not want these executed.
    """
    rows = "operation" if count == 1 else "operations"
    return (
        f"Discard {count} {rows}",
        f"Mark {count} checked {rows} as discarded. They will NOT be executed by a Run, and the reason is stored with them.",
    )


def unique_entities(selection: list[tuple[str, str | None]]) -> list[str]:
    """The DISTINCT sharedIds of a checked (sharedId, language) selection (pure).

    A revert restores the WHOLE entity (its one backup covers every language
    row), so two checked rows of the same entity revert it once — the button's
    count and the confirm dialog speak entities, not rows.
    """
    seen: list[str] = []
    for shared_id, _language in selection:
        if shared_id and shared_id not in seen:
            seen.append(shared_id)
    return seen


def select_all_label(total: int, all_selected: bool) -> str:
    """The whole-result selection button's text (pure).

    The table's header checkbox already selects the current PAGE, so this
    button carries the other meaning: every row matching the current filter,
    across every page. Once that mode is active the same button clears it.
    """
    if all_selected:
        return "Clear selection"
    if not total:
        return "Select all results"
    noun = "result" if total == 1 else "results"
    return f"Select all {total:,} {noun}"


def selection_count(*, all_selected: bool, total: int, page_selected: int, excluded: int) -> int:
    """How many rows the Run button should report (pure).

    A page selection counts the table's checked rows. A whole-result selection
    counts every row matching the filter (``total``) minus the rows the
    operator explicitly unchecked afterwards (``excluded``).
    """
    if all_selected:
        return max(total - excluded, 0)
    return page_selected


def rows_excluding_keys(rows: list[Any], excluded: set[str]) -> list[dict[str, Any]]:
    """The page rows whose ``row_key`` is not excluded from a whole-result selection (pure)."""
    return [row for row in rows if isinstance(row, dict) and str(row.get("row_key")) not in excluded]


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


# How much of an entity title the review table prints before the ellipsis. The
# cell clips with CSS too (a title has no length), but a long one has to be cut
# in the payload as well: an untruncated 300-character document title was what
# pushed the table's total width past the viewport and forced a horizontal
# scroll on every row. 97 characters is roughly two average document references,
# which is as much as the column can hold at the layout's own cap. The sharedId
# is rendered NEXT to the title (not inside it) and the cell clips the pair, so
# the title itself keeps the whole budget.
TITLE_CELL_CHARS: int = 97

# Column widths (CSS px) for the review table's fixed columns. The Entity column
# takes whatever is left and absorbs the shrink; naming the rest here is what
# stops a long Template/Status cell from widening the table past its container.
LANGUAGE_COLUMN: str = "width: 84px"
TEMPLATE_COLUMN: str = "width: 170px"
OPS_COLUMN: str = "width: 190px"
STATUS_COLUMN: str = "width: 110px"
# Four flat icon buttons (Discard/Undo, Revert, Run, Edit) at ~32px each.
ACTIONS_COLUMN: str = "width: 128px"
# The Entity column takes the remainder: ``max-width: 0`` is what lets the column
# actually shrink under the browser's default table layout (a bare percentage does
# not), and the cell's link carries the ellipsis.
TITLE_COLUMN: str = "max-width: 0; width: 100%"


def property_heading(property_name: str | None, op: str, labels: dict[str, str]) -> str:
    """The heading a suggestion's property row shows to the operator (pure).

    A row names its property by the template's property ``name``
    (``document_ref``), which is an identifier; Uwazi — and every operator — reads
    it as the property's ``label`` ("Document Ref"), so the label wins whenever
    the template's properties were resolved. Falls back to the name when they were
    not (an unreachable instance, an unknown template), and to the op for a
    whole-entity row, which has no property at all.
    """
    if property_name is None:
        return op
    if property_name == TITLE_PROPERTY:
        return TITLE_LABELS[TITLE_PROPERTY]
    return labels.get(property_name) or property_name


def property_caption(property_name: str | None, labels: dict[str, str]) -> str:
    """The raw property name shown under the heading, or ``""`` when it adds nothing (pure).

    The caption is what keeps a row identifiable: "Document Ref" is readable, but
    an operator comparing the review against a CSV still needs ``document_ref``,
    so the name rides along — and is dropped when the heading IS the name (no
    label resolved), where a repeat would be noise.
    """
    if property_name is None:
        return ""
    return "" if property_heading(property_name, "update", labels) == property_name else property_name


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
        "editable": suggestion.op in PROPERTY_OPS,
    }


def counterpart_filter(key: str) -> str:
    """The filter control a pick RESETS, because the two exclude each other (pure).

    The Status dropdown and the Discarded dropdown both speak about the
    discarded rows: "discarded" plus "not discarded" is an empty table nobody
    can read, so picking one clears the other. Any other key has no
    counterpart (""), and its filter stands alone.
    """
    return {"status": "discarded", "discarded": "status"}.get(key, "")


def filter_from_controls(
    *,
    status: str = "",
    discarded: str = "",
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
    trivial. ``discarded`` is the page's tri-state "Discarded" select: "" (any),
    "yes" (only the discarded rows) or "no" (only the rows still to execute).
    ``shared_ids`` is the table's checkbox selection passed through verbatim
    (``None`` = no constraint, ``[]`` = match nothing).
    """
    return SuggestionFilter(
        status=SuggestionStatus(status) if status else None,
        discarded={"yes": True, "no": False}.get(discarded),
        op=op or None,
        property_name=property_name or None,
        match=match or None,  # type: ignore[arg-type] — validated by the pydantic Literal
        text=(text or "").strip() or None,
        accepted_only=accepted_only,
        shared_ids=shared_ids,
        limit=limit,
        offset=offset,
    )


def entity_is_discarded(item: EntityReviewRow) -> bool:
    """True when EVERY row of the entity group is discarded (pure).

    The review table's rows are OPERATIONS (one entity in one language), and the
    Discard action marks a whole operation: "the end user does not want this
    executed". A group whose rows merely SPAN states (some discarded, some
    still pending) is not discarded — work remains, so the operator has to see
    it under "not discarded" too.
    """
    return item.proposal_count > 0 and item.discarded >= item.proposal_count


def run_status(item: EntityReviewRow) -> tuple[str, str]:
    """The entity's Run state as ``(label, color)`` — the table's Status column (pure).

    ``runned`` once EVERY recorded row of the entity is APPLIED (written to
    Uwazi); ``reverted`` once every row is REVERTED — a revert undid the writes,
    so the operator can run the entity again, row by row or whole. ``discarded``
    once every row is DISCARDED — the operator refused to execute this
    operation, so it must not read as work still to do. Anything else (a row
    still to review, a mix of applied and reverted) is ``pending``: work
    remains.
    """
    if item.applied >= item.proposal_count:
        label = "runned"
    elif item.reverted >= item.proposal_count:
        label = "reverted"
    elif entity_is_discarded(item):
        label = "discarded"
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


def entity_row(item: EntityReviewRow, uwazi_url: str | None = None, backup_created_at: Any = None) -> dict[str, Any]:
    """The entity table's row contract (pure). Colors resolved here, not in Vue.

    One row per affected entity IN ONE LANGUAGE (the table is the
    checkbox-selection surface the bulk Run action reads), aggregated from the
    entity's suggestion rows by the store. Columns: Entity (the sharedId and
    the title, with the open-in-Uwazi icon at its end), Language, Template,
    Ops, Status (runned/pending), and the Run/Edit/Revert row actions — no
    separate sharedId column, no change-count columns, and no separate Uwazi
    column.

    ``title`` is the entity title cut to :data:`TITLE_CELL_CHARS`; the
    untruncated text rides along as ``title_full`` for the cell's tooltip: a
    full document title is far wider than the column it sits in, and letting it
    size the cell is what made the table overflow horizontally. The sharedId is
    a field of its own (``shared_id``): the cell shows it as plain text BESIDE
    the title, so the operator can select and copy the id on its own — it used
    to be glued to the title inside parentheses, which made both the copy and
    the mouse selection awkward.

    ``uwazi_url`` is the controlled instance's base URL (None = unconfigured, so
    the row carries no link). ``row_key`` is the entity+language identity, so the
    same entity in two languages is two distinct checkable rows.

    ``revert_tooltip`` is the per-row Revert button's tooltip: the backup's
    creation date+time ("entity backup creation …"), present only when the run
    holds a backup for this entity (a written-then-revertable row).

    ``discarded`` says whether the OPERATION was marked as discarded (every one
    of its rows discarded): the Actions cell swaps its Discard button for the
    Undo one, and the operator can filter the table by this state.
    """
    status_label, status_color = run_status(item)
    # Per-entity revert is an IN-PLACE restore of the live row. It is therefore
    # only offered for entities the run merely MODIFIED: a `delete` row's undo is
    # a re-create (a fresh sharedId, files and connection rows) and a `create`
    # row's undo is a deletion — both are ordered run-level operations. A merge
    # row's undo is likewise ordered: the target was modified/created AND its
    # sources deleted, so restoring the target alone would not restore them.
    in_place_revertable = status_label in ("runned", "reverted") and not any(
        op in ("delete", "create", "update_and_delete", "create_and_delete") for op in item.ops
    )
    title = item.entity_title or item.shared_id
    try:
        revert_tooltip = (
            f"entity backup creation {backup_created_at:%Y-%m-%d %H:%M}" if backup_created_at is not None else ""
        )
    except (ValueError, TypeError):
        revert_tooltip = ""
    return {
        "row_key": entity_language_key(item.shared_id, item.language),
        "shared_id": item.shared_id,
        "title": truncate(title, TITLE_CELL_CHARS),
        "title_full": title,
        "language": item.language or "",
        "template": truncate(", ".join(item.template_names) or "—", 28),
        "ops": ", ".join(item.ops),
        "op_colors": [OP_COLORS.get(op, "grey") for op in item.ops],  # used by the slot
        "url": entity_open_url(item, uwazi_url),  # used by the title cell's open-in-Uwazi icon
        "run_status": status_label,
        "run_status_color": status_color,  # used by the slot
        "discarded": entity_is_discarded(item),  # drives the Discard/Undo button
        "revertable": in_place_revertable,  # drives the Revert icon's visibility
        "revert_tooltip": revert_tooltip,  # drives the Revert icon's tooltip
    }


def accuracy_label(exact: int, mismatch: int) -> str:
    """The accuracy figure shown in the header, or 'n/a' when nothing compares (pure)."""
    denominator = exact + mismatch
    return f"{100 * exact / denominator:.1f}%" if denominator else "n/a"


# --- drift banner ------------------------------------------------------------

DRIFT_STATUS_COLORS: dict[str, str] = {
    "unknown": "grey-6",
    "valid": "green",
    "drifted": "orange",
    "error": "red",
    "checking": "blue",
}


def drift_banner_text(*, status: str, detail: str | None, checking: bool = False) -> str:
    """The status line above the review table (pure).

    ``checking=True`` wins over any stored verdict (a check is running right
    now); otherwise the stored verdict speaks, with the check's detail after
    an em dash.
    """
    if checking:
        return "Checking drift…" if not detail else f"Checking drift… (last check: {detail})"
    text = {
        "unknown": "Drift not checked yet",
        "valid": "Up to date — no changes in Uwazi since the last sync",
        "drifted": "Out of date — Uwazi changed since the last sync",
        "error": "Drift check failed",
    }.get(status, "Drift not checked yet")
    if detail:
        return f"{text} — {detail}"
    return text


def drift_banner_time(checked_at: Any) -> str:
    """The checked-at suffix ('· checked 12:34'), '' when never checked (pure)."""
    if checked_at is None:
        return ""
    try:
        return f" · checked {checked_at:%H:%M}"
    except (ValueError, TypeError):
        return ""


__all__ = [
    "DRIFT_STATUS_COLORS",
    "ACTIONS_COLUMN",
    "MATCH_COLORS",
    "OPENABLE_OPS",
    "OP_COLORS",
    "OPS_COLUMN",
    "RUN_OP_PHRASES",
    "RUN_STATUS_COLORS",
    "STATUS_COLUMN",
    "STATUS_COLORS",
    "TEMPLATE_COLUMN",
    "TITLE_CELL_CHARS",
    "TITLE_COLUMN",
    "accuracy_label",
    "bulk_discard_label",
    "counterpart_filter",
    "discard_confirmation",
    "drift_banner_text",
    "drift_banner_time",
    "entity_is_discarded",
    "entity_row",
    "entity_open_url",
    "entity_language_key",
    "filter_from_controls",
    "match_label",
    "property_caption",
    "bulk_revert_label",
    "property_heading",
    "prune_selection",
    "revert_entity_confirmation",
    "rows_excluding_keys",
    "unique_entities",
    "run_button_label",
    "run_confirmation",
    "run_status",
    "select_all_label",
    "selection_count",
    "suggestion_to_row",
    "truncate",
]
