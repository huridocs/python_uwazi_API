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
from uwazi_admin_agent.domain.metadata_diff import (
    format_metadata_value,
    format_property_value,
    render_property_value,
)
from uwazi_admin_agent.domain.suggestion import (
    FILE_OPS,
    MATCHED_OP,
    PROPERTY_OPS,
    SKIPPED_OP,
    TITLE_PROPERTY,
    EntityReviewRow,
    Suggestion,
    SuggestionFilter,
    SuggestionStatus,
    entity_language_key,
)

# Column/table op and status colors (the UI maps these onto Quasar badges).
# Badges render WHITE text on the color, so every value here is a DARK shade:
# the base palette colors (green, orange, amber, grey) sit at ~2:1 contrast
# with white and are hard to read. These shades keep at least ~4.5:1.
STATUS_COLORS: dict[str, str] = {
    "pending": "grey-8",
    "discarded": "pink-10",
    "applied": "blue-9",
    "reverted": "indigo",
}

# The OPERATION badges use their own hue family, deliberately disjoint from
# STATUS_COLORS: an operation and a status sit in two adjacent columns, and
# sharing a color made "update" and "applied" read as the same fact. The
# statuses keep the deep state colors (grey pending, blue applied, indigo
# reverted, pink discarded) while every operation is named by a mid-tone hue.
OP_COLORS: dict[str, str] = {
    "update": "teal-10",
    "create": "cyan-10",
    "delete": "red",
    "publish": "light-green-9",
    "unpublish": "orange-10",
    "set_publish_status": "amber-10",
    "create_relationships": "purple",
    "move_files": "blue-grey-9",
    "update_and_delete": "deep-purple",
    "create_and_delete": "red-10",
    "info": "grey-7",
    "matched": "light-green-10",
    "skipped": "grey-7",
    "validate": "purple-7",
}

# The human labels of the Operation filter. Kept beside the colors so the control
# and the badges agree on what each operation is called.
OP_LABELS: dict[str, str] = {
    "update": "update",
    "create": "create",
    "delete": "delete",
    "publish": "publish",
    "unpublish": "unpublish",
    "set_publish_status": "set publish status",
    "create_relationships": "create relationships",
    "move_files": "move files",
    "update_and_delete": "merge (update + delete)",
    "create_and_delete": "merge (create + delete)",
    "info": "information",
    "matched": "matches Uwazi",
    "skipped": "excluded",
    "validate": "validate",
}

MATCH_COLORS: dict[str, str] = {
    "exact": "green-9",
    "mismatch": "red-9",
    "new": "grey-7",
    "mixed": "deep-orange-10",
    "n/a": "blue-grey-7",
}

# The entity table's Run column: has this entity's recorded work been written
# to Uwazi yet? Reuses the per-row statuses' colors ('applied' blue, 'pending'
# grey, 'reverted' indigo) so one state reads the same everywhere — plus the
# 'discarded' red, the state an operation the operator refused to execute ends
# up in.
RUN_STATUS_COLORS: dict[str, str] = {
    "applied": "blue-9",
    "reverted": "indigo",
    "pending": "grey-8",
    "discarded": "pink-10",
    # An operation whose parts disagree (some written, some still to write).
    # Orange is work-in-progress, distinct from both the blue "applied" and the
    # grey "pending" it sits between.
    "mixed": "deep-orange-9",
    "info": "blue-grey-8",
    "matched": "light-green-10",
    "skipped": "grey-7",
    "validate": "purple-7",
    # A validation operation's parent badge states the comparison verdict its
    # parts carry, so the operator reads "match"/"mismatch" on the collapsed row
    # exactly as on each part. Same hues as MATCH_COLORS so the two levels agree.
    "match": "green-9",
    "mismatch": "red-9",
}


# Operations whose entity ALREADY EXISTS in Uwazi, so the review row can link to its
# public page. A create row still holds its dry-created placeholder, so
# opening it would land on nothing.
OPENABLE_OPS: frozenset[str] = frozenset({"update", "delete", "update_and_delete", "move_files", MATCHED_OP, SKIPPED_OP})

# Operations whose per-entity undo is NOT an in-place restore of one live entity, so
# their rows never offer the Revert icon: a `create_and_delete` merge's target is only
# a ``dry-created-N`` placeholder in the review rows (Uwazi has no such entity). A
# plain `create` IS undone from its row — the undo deletes the entity the run minted
# — a plain `delete` IS undone from its row too (the undo re-creates the entity from
# its backup with a fresh sharedId), and an `update_and_delete` merge restores the
# target and re-creates the sources it deleted, so none of those three is here.
RUN_LEVEL_ONLY_OPS: frozenset[str] = frozenset({"create_and_delete"})

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
    "move_files": "Add files",
    "update_and_delete": "Merge (update + delete)",
    "create_and_delete": "Merge (create + delete)",
    "matched": "Already matches Uwazi",
    "skipped": "Excluded",
    "validate": "Validate",
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
    entities = "suggestion" if count == 1 else "suggestions"
    title = f"Run {count} {entities}"
    if languages:
        title = f"{title} ({', '.join(languages)})"
    phrases = [RUN_OP_PHRASES[op] for op in ops or [] if op in RUN_OP_PHRASES]
    action = " + ".join(dict.fromkeys(phrases)) or "Write"
    # The per-kind breakdown: the operator sees HOW the rows split before they
    # consent, not only a bare count ("Update ×2 · Delete ×1").
    counts = {op: (ops or []).count(op) for op in dict.fromkeys(ops or []) if op in RUN_OP_PHRASES}
    breakdown = " · ".join(f"{RUN_OP_PHRASES[op]} ×{n}" for op, n in counts.items())
    # A standalone file copy adds bytes to the target; Revert restores entity
    # snapshots and cannot trace those bytes back to a source this run deleted,
    # so the one thing the dialog must warn about is that they stay behind.
    suffix = f" Breakdown: {breakdown}." if breakdown else ""
    if FILE_OPS.intersection(ops or []):
        return title, f"{action} {count} {entities} in Uwazi now. The copied files stay on the target.{suffix}"
    return title, f"{action} {count} {entities} in Uwazi now.{suffix}"


def revert_entity_confirmation(
    shared_id: str,
    tooltip: str = "",
    merge: bool = False,
    created: bool = False,
    deleted: bool = False,
) -> tuple[str, str]:
    """``(title, message)`` for the per-row Revert dialog (pure).

    The restore rolls the WHOLE entity back — every language row the backup
    captured — because the rows share one backup per entity; the message says
    so (and shows the backup's creation time when one exists) so reverting the
    ``es`` row never silently rewrites the ``en`` text.

    ``merge`` (an ``update_and_delete`` row) makes the undo bigger than this
    entity's own backup: the sources the merge absorbed are re-created from
    their backups with NEW sharedIds, and the files the merge copied onto the
    target are removed again (verified byte-for-byte against the sources'
    backups) — so the message spells both out.

    ``created`` says the row's entity did not exist before the Run: there is no
    backup to restore, so the undo DELETES it. The dialog says that plainly —
    deleting an entity must never be described as a restore.

    ``deleted`` says the Run DELETED the entity: there is no live row to update,
    so the undo RE-CREATES it from the backup. That mints a NEW sharedId (Uwazi
    cannot resurrect the old one) and re-uploads its files — the dialog must not
    promise that the old id will come back.
    """
    if created:
        return (
            "Revert create",
            f"Delete {shared_id}: this run created it, so undoing the creation removes the entity "
            "and every file it was created with. Its review rows go back to pending.",
        )
    when = f" made {tooltip.removeprefix('entity backup creation ')}" if tooltip else ""
    restored = f"Restore {shared_id} to its backup{when}. ALL languages of this entity are restored (they share one backup)."
    if deleted:
        return (
            "Revert delete",
            f"Re-create {shared_id} from its backup{when}: this run deleted it, so undoing the delete writes the "
            "backup back as a NEW entity with a fresh sharedId (its old id cannot be reused), and re-uploads the "
            "files the backup captured. ALL languages restored.",
        )
    if not merge:
        return "Revert entity", restored
    return (
        "Revert merge",
        restored
        + " The source entities this merge absorbed are re-created from their backups"
        + " (with new sharedIds). The files it copied onto the target are removed again.",
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
    how many checked suggestions the Discard will mark as not-to-be-executed.
    """
    return f"Discard ({count})"


def discard_confirmation(count: int) -> tuple[str, str]:
    """``(title, message)`` for the bulk Discard dialog (pure).

    The dialog is consent, not explanation: it says how many checked operations
    will be marked as discarded and that a Run will not execute them. The reason
    itself is asked in the same dialog (it is stored with the rows), so the
    operator can say WHY the end user does not want these executed.
    """
    noun = "suggestion" if count == 1 else "suggestions"
    return (
        f"Discard {count} {noun}",
        f"Mark {count} checked {noun} as discarded. They will NOT be applied by a Run, and the reason is stored with them.",
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


def expansion_mode(rows: list[Any], expanded: set[str]) -> str:
    """The page's expansion as ``"all"``, ``"none"`` or ``"mixed"`` (pure).

    ``rows`` is the table's current page and ``expanded`` the table's
    ``expanded`` prop (row keys). ``"all"`` and ``"none"`` are the page-wide
    states Expand all / Collapse all produce; those are the modes a page flip
    carries forward. ``"mixed"`` is a per-row state, which stays with the page
    it was made on.
    """
    keys = {str(row["row_key"]) for row in rows if isinstance(row, dict) and "row_key" in row}
    if keys and keys <= expanded:
        return "all"
    return "mixed" if keys & expanded else "none"


def expanded_keys_for(rows: list[Any], mode: str, current: set[str] = frozenset()) -> set[str]:
    """The ``expanded`` prop a freshly loaded page starts with (pure).

    ``"all"`` expands every row of the new page, so pressing Next after Expand
    all lands on a fully expanded page; ``"none"`` collapses them all, so
    Collapse all carries across pages the same way. ``"mixed"`` is a per-row
    state: the rows of the SAME page keep their expansion (a poll reload must
    not close an open row), while keys that are not on the new page — like the
    previous page's — drop out.
    """
    keys = {str(row["row_key"]) for row in rows if isinstance(row, dict) and "row_key" in row}
    if mode == "all":
        return keys
    if mode == "none":
        return set()
    return keys & set(current)


def truncate(value: Any, limit: int = 80, property_type: str | None = None) -> str:
    """Render any value as one display line, truncated with an ellipsis (pure).

    ``property_type`` (the template's ``date``/``multidate``/...) makes date
    values render as calendar dates instead of raw epoch timestamps.
    """
    text = format_property_value(value, property_type) if property_type else format_metadata_value(value)
    return text if len(text) <= limit else text[:limit] + "…"


def full_value(value: Any, property_type: str | None = None) -> str:
    """Render any value in FULL, newlines preserved (pure; the entity window).

    The review TABLE truncates (its cells are one line), but the entity detail
    window must show the whole paragraph: truncating there cut text/richtext
    values after 200 characters. Rendering is untruncated; the caller wraps the
    label with ``white-space: pre-wrap`` so line breaks survive.
    """
    return render_property_value(value, property_type)


# How much of an entity title the review table prints before the ellipsis. The
# cell clips with CSS too (a title has no length), but a long one has to be cut
# in the payload as well: an untruncated 300-character document title was what
# pushed the table's total width past the viewport and forced a horizontal
# scroll on every row. 97 characters is roughly two average document references,
# which is as much as the column can hold at the layout's own cap. The sharedId
# is rendered NEXT to the title (not inside it) and the cell clips the pair, so
# the title itself keeps the whole budget.
TITLE_CELL_CHARS: int = 97

# How much of a row's recorded INPUT TEXT the review table prints. The cell
# wraps, but an unbounded passage (a whole page) would push everything else off
# the screen, so the payload carries a window instead. 2,500 characters is a
# substantial passage: enough to judge the extraction without opening the
# document, while still bounding the row's height.
CONTEXT_CELL_CHARS: int = 2_500

# Column widths (CSS px) for the review table's fixed columns. The Target column
# takes whatever is left and absorbs the shrink; naming the rest here is what
# stops a long Template/Status cell from widening the table past its container.
LANGUAGE_COLUMN: str = "width: 84px"
TEMPLATE_COLUMN: str = "width: 170px"
OPS_COLUMN: str = "width: 190px"
STATUS_COLUMN: str = "width: 110px"
# The Modified column writes "YYYY-MM-DD HH:MM": one fixed-width slot keeps the
# dates lined up down the column and stops them from squeezing the Target cell.
MODIFIED_COLUMN: str = "width: 142px"

# The review table's always-visible sort control: the value is the
# ``SuggestionFilter.sort_by`` field, the label is what the operator reads.
OPERATION_SORT_OPTIONS: dict[str, str] = {
    "recorded": "Original order",
    "modified": "Modified",
    "title": "Target",
    "template": "Template",
    "ops": "Operations",
    "status": "Status",
}

# The review table's sortable headers. Quasar reports a header click with the
# COLUMN NAME, while the store sorts on a SuggestionFilter key; this is the
# bridge between the two. "recorded" (the script's order) has no column header:
# the table starts in that order, and the other five headers cover every other
# sort key the store supports.
SORT_KEY_BY_COLUMN: dict[str, str] = {
    "title": "title",
    "template": "template",
    "ops": "ops",
    "run_status": "status",
    "modified": "modified",
}
COLUMN_BY_SORT_KEY: dict[str, str] = {key: column for column, key in SORT_KEY_BY_COLUMN.items()}


def sort_from_header_click(sort_by: str, sort_desc: bool, column: str) -> tuple[str, bool] | None:
    """The new (sort_by, sort_desc) after clicking a table header (pure).

    A click on a NEW column picks that column's natural first direction —
    Modified newest-first, every other column A→Z — and a click on the CURRENT
    column flips the direction. There is no third, unsorted state: clicking the
    same header again and again keeps reversing the order. Headers that are not
    sortable (and unknown column names) return None and leave the order alone.
    """
    key = SORT_KEY_BY_COLUMN.get(column)
    if key is None:
        return None
    if key == sort_by:
        return key, not sort_desc
    return key, key == "modified"


def review_sort_pagination(sort_by: str, sort_desc: bool, rows_number: int = 0) -> dict[str, Any]:
    """The QTable pagination object that mirrors the server-side sort (pure).

    ``rowsNumber`` puts Quasar in server mode: it then trusts the rows we hand
    it (already ordered by SQL across the WHOLE filtered set) and draws the sort
    arrow from ``sortBy``/``descending`` without re-sorting the current page
    locally. A local re-sort would disagree with the store's status/op ranking.
    """
    return {
        "rowsPerPage": 0,
        "rowsNumber": rows_number,
        "sortBy": COLUMN_BY_SORT_KEY.get(sort_by, ""),
        "descending": bool(sort_desc),
    }


# Four flat icon buttons (Discard/Undo, Revert, Run, Edit) at ~32px each.
ACTIONS_COLUMN: str = "width: 150px"
# The Target column takes the remainder: ``max-width: 0`` is what lets the column
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
        "op_color": OP_COLORS.get(suggestion.op, "grey-7"),
        "property": suggestion.property_name or "—",
        "proposed": truncate(suggestion.proposed_value),
        "current": truncate(suggestion.current_value),
        "match": label,
        "match_color": MATCH_COLORS.get(label, "grey-7"),
        "status": suggestion.status.value,
        "status_color": STATUS_COLORS.get(suggestion.status.value, "grey-7"),
        "editable": suggestion.op in PROPERTY_OPS,
    }


# The Status filter also offers the validation rows' comparison verdicts. The
# values are the table's badge words; the mapping says which store verdicts each
# one selects. "mismatch" includes "new" because a validation row with no
# proposal has no current verdict but is still NOT a match.
VALIDATION_MATCH_FILTERS: dict[str, tuple[str, ...]] = {
    "match": ("exact",),
    "mismatch": ("mismatch", "new"),
}


def _selection(value: str | list[str] | tuple[str, ...] | None) -> list[str] | None:
    """Normalize a select value to a list (``None`` when nothing is selected)."""
    if not value:
        return None
    if isinstance(value, str):
        return [value]
    return list(value)


def filter_options(
    labels: dict[str, str],
    counts: dict[str, int],
    selected: list[str] | str | None,
    *,
    drop_empty: bool = True,
) -> dict[str, str]:
    """Options of a COUNTED filter control: only values the rows still match.

    ``labels`` fixes the display order and wording. The label reads
    ``update (1,204)``.

    ``counts`` is the FACETED count (see
    :meth:`~...suggestion_store.SuggestionStorePort.op_status_counts`): it
    already reflects the other filters, so ``drop_empty`` removes the options
    that would select nothing under them instead of offering a dead end.

    A SELECTED value is always kept, even when its count dropped to zero: a
    rebuild after an action must not silently clear a filter that is still set.
    It keeps its ``(0)`` label, which tells the operator the selection is empty
    rather than hiding the control's own state.
    """
    selected_values = _selection(selected) or []
    options: dict[str, str] = {}
    for value, label in labels.items():
        count = counts.get(value, 0)
        if value in selected_values:
            options[value] = f"{label} ({count:,})"
        elif value in counts and not (drop_empty and count == 0):
            options[value] = f"{label} ({count:,})"
    for value in selected_values:
        if value not in options:
            options[value] = f"{value} ({counts.get(value, 0):,})"
    return options


def property_filter_options(
    labels: dict[str, str],
    counts: dict[str, int],
    selected: str,
) -> dict[str, str]:
    """The Property dropdown's options: present properties, each with its count (pure).

    The first option is the unfiltered ``"any property"`` (no count — it is the
    absence of a constraint). The others come from ``counts``, which is the
    faceted count under the rest of the filter, so a property whose every row
    the other filters removed is not offered. The SELECTED property is kept even
    at zero, so the control still shows what is applied.
    """
    options: dict[str, str] = {"": "any property"}
    for name, count in counts.items():
        if count <= 0 and name != selected:
            continue
        options[name] = f"{labels.get(name, name)} ({count:,})"
    if selected and selected not in options:
        options[selected] = f"{labels.get(selected, selected)} (0)"
    return options


def language_filter_options(
    counts: dict[str, int],
    selected: str,
) -> dict[str, str]:
    """The Language dropdown's options, counted like the Property one (pure)."""
    options: dict[str, str] = {"": "any language"}
    for language, count in counts.items():
        if count <= 0 and language != selected:
            continue
        options[language] = f"{language} ({count:,})"
    if selected and selected not in options:
        options[selected] = f"{selected} (0)"
    return options


def filter_from_controls(
    *,
    status: str | list[str] = "",
    op: str | list[str] = "",
    property_name: str = "",
    text: str = "",
    language: str = "",
    offset: int = 0,
    limit: int = 25,
    shared_ids: list[str] | None = None,
    sort_by: str = "recorded",
    sort_desc: bool = False,
) -> SuggestionFilter:
    """Build the server-side filter from the page's raw control values (pure).

    Empty selections mean "no constraint" — the single-value selects use "" for
    their "all" option and the multi-selects use an empty list, and translating
    both here keeps the page's handlers trivial. ``status``/``op`` accept one
    value or a list (the Operation and Status controls are multi-selects). The
    operation ``"validate"`` is the display name of the run's holdout rows, so
    the store receives it as an op and resolves it to ``is_validation``.
    ``language`` narrows the SUGGESTIONS to one written language (the
    language-independent rows are their own group and are not matched).
    ``shared_ids`` is the table's checkbox selection passed through verbatim
    (``None`` = no constraint, ``[]`` = match nothing). ``sort_by``/``sort_desc``
    are the always-visible sort control's state; the store orders the WHOLE
    filtered set, so the order holds across pages.
    """
    statuses = _selection(status)
    match_verdicts: list[str] = []
    if statuses:
        # The Status control carries both lifecycle statuses and the holdout
        # verdicts ("match"/"mismatch"); the store takes the latter as its own
        # validation-scoped filter, so it can never leak into regular rows.
        for value in statuses:
            match_verdicts.extend(VALIDATION_MATCH_FILTERS.get(value, ()))
        statuses = [value for value in statuses if value not in VALIDATION_MATCH_FILTERS]
    ops = _selection(op)
    return SuggestionFilter(
        status=[SuggestionStatus(s) for s in statuses] if statuses else None,
        matches=list(dict.fromkeys(match_verdicts)) or None,
        op=ops,
        property_name=property_name or None,
        text=(text or "").strip() or None,
        languages=[language] if language else None,
        shared_ids=shared_ids,
        limit=limit,
        offset=offset,
        sort_by=sort_by,  # type: ignore[arg-type] — validated by the pydantic Literal
        sort_desc=sort_desc,
    )


def entity_is_discarded(item: EntityReviewRow) -> bool:
    """True when EVERY row of the entity group is discarded (pure).

    The review table's rows are ENTITIES, and the Discard action marks a whole
    operation: "the end user does not want this executed". A group whose rows
    merely SPAN states (some discarded, some still pending) is not discarded —
    work remains, so the operator has to see it under "not discarded" too.
    """
    return item.proposal_count > 0 and item.discarded >= item.proposal_count


def run_status(item: EntityReviewRow) -> tuple[str, str]:
    """The entity's Run state as ``(label, color)`` — the table's Status column (pure).

    ``applied`` once EVERY recorded row of the entity is APPLIED (written to
    Uwazi); ``reverted`` once every row is REVERTED — a revert undid the writes,
    so the operator can run the entity again, row by row or whole. ``discarded``
    once every row is DISCARDED — the operator refused to execute this
    operation, so it must not read as work still to do. Anything else (a row
    still to review, a mix of applied and reverted) is ``pending``: work
    remains.
    """
    if item.applied >= item.proposal_count:
        label = "applied"
    elif item.reverted >= item.proposal_count:
        label = "reverted"
    elif entity_is_discarded(item):
        label = "discarded"
    else:
        label = "pending"
    return label, RUN_STATUS_COLORS[label]


def entity_open_url(item: EntityReviewRow, uwazi_url: str | None) -> str | None:
    """The instance's public page for this entity, or None when it has none.

    Uwazi serves an entity at ``/<language>/entity/<sharedId>`` — the same URL
    form the entity window uses — and the ``<language>`` segment is the ROW's
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
    the title, with the open-in-Uwazi icon at its end), Template, Language,
    Operations, Status (applied/pending), and the Run/Edit/Revert row actions — no
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
    # Per-entity revert is an IN-PLACE restore of the live row. It is offered for
    # entities the run merely MODIFIED, for a `delete` (whose undo re-creates the
    # entity from its backup) and for an `update_and_delete` merge, whose row owns
    # the target's write AND the source deletes — its undo restores the target,
    # re-creates the absorbed sources and removes the files it copied onto the
    # target. Everything in RUN_LEVEL_ONLY_OPS is undone by the run-level Revert
    # instead, and a standalone file copy still appends bytes the revert cannot
    # trace back to a source this run deleted.
    in_place_revertable = status_label in ("applied", "reverted") and not any(
        op in RUN_LEVEL_ONLY_OPS or op in FILE_OPS for op in item.ops
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
        "op_colors": [OP_COLORS.get(op, "grey-7") for op in item.ops],  # used by the slot
        "url": entity_open_url(item, uwazi_url),  # used by the title cell's open-in-Uwazi icon
        "run_status": status_label,
        "run_status_color": status_color,  # used by the slot
        "discarded": entity_is_discarded(item),  # drives the Discard/Undo button
        "revertable": in_place_revertable,  # drives the Revert icon's visibility
        "revert_tooltip": revert_tooltip,  # drives the Revert icon's tooltip
    }


# The currency the LLM costs are denominated in: the same money the cost
# dialog's cap field is labelled with ("Max cost ($)").
CURRENCY_SYMBOL: str = "$"


def format_cost(value: float) -> str:
    """One amount of money, in the operator's currency (pure).

    The figure is money a person reads at a glance: the currency symbol, the
    whole part and TWO decimals — the per-mtok fractions the domain's own
    formatter prints with four decimals are noise on a card.
    """
    return f"{CURRENCY_SYMBOL}{value:.2f}"


def cost_card_value(spent: float, budget: float) -> str:
    """The LLM budget card's value: the spend against the cap (pure).

    One line, read as "what has been spent of what was allowed".
    """
    return f"{format_cost(spent)} / {format_cost(budget)}"


def pending_suboperation_count(counts: dict[str, int]) -> int:
    """The header's Pending card: every suggestion still waiting to be written (pure)."""
    return int(counts.get(f"subs_{SuggestionStatus.PENDING.value}", 0))


# --- performance line --------------------------------------------------------

# The validation states that carry a measurement worth a row on the review
# page. "none" (the run has no test set at all) has nothing to show: the row
# asked the operator to go back to the wizard, which is not a result, and it
# took the top of the page from the table they came for.
_MEASURED_VALIDATION_STATES: frozenset[str] = frozenset({"fresh", "pending", "stale", "contaminated"})


def performance_visible(summary: dict[str, Any]) -> bool:
    """Whether the review page shows the performance line for this run (pure).

    The line exists to show a SCORE — a measured share of a held-out set, or
    the state of one that has not been scored yet. A run with no test set has
    no score, no set and no property to name, so the line is omitted entirely
    (the wizard's "Test set" step and the entity window's "Add to validation"
    are where a set is configured). Hiding it is what the operator asked for:
    an idle "Performance — / No test set / Add a test set in the wizard" strip
    above every table reads as a broken feature, not as a missing option.
    """
    if summary.get("set") is None:
        return False
    return str(summary.get("state") or "none") in _MEASURED_VALIDATION_STATES


# --- drift banner ------------------------------------------------------------

# The verdict's color, used for BOTH the banner icon and the banner TEXT.
# "drifted" is red on purpose: an out-of-date list is the one state the
# operator must not miss. "valid" is grey, not green: being up to date is the
# resting state, so it must not compete with the red warning for attention.
DRIFT_STATUS_COLORS: dict[str, str] = {
    "unknown": "grey-7",
    "valid": "grey-7",
    "drifted": "red",
    "error": "red",
    "checking": "blue",
}


def drift_banner_text(
    *,
    status: str,
    detail: str | None,
    checking: bool = False,
    syncing: bool = False,
) -> str:
    """The status line above the review table (pure).

    ``syncing=True`` wins over everything (the table is being re-read);
    ``checking=True`` wins over any stored verdict (a check is running right
    now); otherwise the stored verdict speaks, with the check's detail after
    an em dash.
    """
    if syncing:
        return "Syncing with Uwazi…"
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


def drift_text_classes(color: str) -> str:
    """The drift banner TEXT's class list for one verdict color (pure).

    The banner's text carries the verdict color too, not just its icon: a
    red "Out of date" line is the whole point of the banner. ``color`` is a
    Quasar/Tailwind color name (``grey-7`` for the resting state).
    """
    return f"text-body2 text-{color} grow"


def drift_banner_time(checked_at: Any) -> str:
    """The checked-at suffix ('· checked 12:34'), '' when never checked (pure)."""
    if checked_at is None:
        return ""
    try:
        return f" · checked {checked_at:%H:%M}"
    except (ValueError, TypeError):
        return ""


def section_toggle_label(section: str, visible: bool) -> str:
    """The label of a section's show/hide button (pure).

    The button states what the click WILL do: a hidden section's button offers
    to show it, a visible one's offers to hide it again.
    """
    return f"Hide {section}" if visible else f"Show {section}"


def table_update_label(updated_at: Any) -> str:
    """When the list was last synced with Uwazi ("" when it never has been).

    A never-synced run says nothing here: the drift banner already states the
    outcome of the last check, and a second sentence about the absence of a
    sync only added noise to the line.
    """
    if updated_at is None:
        return ""
    try:
        return f"Synced with Uwazi {updated_at:%Y-%m-%d %H:%M} UTC"
    except (ValueError, TypeError):
        return "Synced with Uwazi"


def modified_label(value: Any) -> str:
    """When the operator last acted on this operation, as the table writes it (pure).

    Empty until an action actually happens: a freshly recorded row has no review
    activity, so the column stays blank instead of showing the recording time
    as if the operator had touched it.
    """
    if value is None:
        return ""
    try:
        return f"{value:%Y-%m-%d %H:%M}"
    except (ValueError, TypeError):
        return ""


def sort_direction_label(sort_by: str, sort_desc: bool) -> str:
    """The text of the sort-direction toggle (pure).

    It names the order the operator is looking at, not the click: "Newest first"
    on a Modified sort says the top row is the most recently touched one.
    """
    if sort_by == "modified":
        return "Newest first" if sort_desc else "Oldest first"
    if sort_by == "recorded":
        return "Last recorded first" if sort_desc else "First recorded first"
    return "Z → A" if sort_desc else "A → Z"


def row_neighbours(
    pairs: list[tuple[str, str | None]],
    current: tuple[str, str | None],
) -> tuple[tuple[str, str | None] | None, tuple[str, str | None] | None]:
    """The previous/next rows around ``current`` for the entity window (pure).

    Navigation does NOT wrap: the first row has no previous and the last has no
    next. The window hides the button that has nowhere to go, so a result with a
    single row shows no navigation buttons at all instead of two buttons that
    reload the same entity. A current row missing from the filtered list has no
    neighbours to walk.
    """
    try:
        index = pairs.index(current)
    except ValueError:
        return None, None
    previous = pairs[index - 1] if index > 0 else None
    following = pairs[index + 1] if index + 1 < len(pairs) else None
    return previous, following


__all__ = [
    "DRIFT_STATUS_COLORS",
    "ACTIONS_COLUMN",
    "CURRENCY_SYMBOL",
    "MATCH_COLORS",
    "MATCHED_OP",
    "MODIFIED_COLUMN",
    "OPENABLE_OPS",
    "OP_COLORS",
    "OP_LABELS",
    "OPERATION_SORT_OPTIONS",
    "COLUMN_BY_SORT_KEY",
    "SORT_KEY_BY_COLUMN",
    "OPS_COLUMN",
    "RUN_OP_PHRASES",
    "RUN_STATUS_COLORS",
    "SKIPPED_OP",
    "STATUS_COLUMN",
    "STATUS_COLORS",
    "TEMPLATE_COLUMN",
    "CONTEXT_CELL_CHARS",
    "TITLE_CELL_CHARS",
    "TITLE_COLUMN",
    "bulk_discard_label",
    "cost_card_value",
    "discard_confirmation",
    "drift_banner_text",
    "drift_banner_time",
    "drift_text_classes",
    "entity_is_discarded",
    "entity_row",
    "entity_open_url",
    "entity_language_key",
    "filter_from_controls",
    "filter_options",
    "format_cost",
    "match_label",
    "modified_label",
    "bulk_revert_label",
    "property_heading",
    "pending_suboperation_count",
    "performance_visible",
    "expanded_keys_for",
    "expansion_mode",
    "prune_selection",
    "revert_entity_confirmation",
    "rows_excluding_keys",
    "row_neighbours",
    "unique_entities",
    "run_button_label",
    "run_confirmation",
    "run_status",
    "review_sort_pagination",
    "section_toggle_label",
    "select_all_label",
    "selection_count",
    "sort_direction_label",
    "sort_from_header_click",
    "suggestion_to_row",
    "table_update_label",
    "truncate",
]
