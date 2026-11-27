"""Pure view helpers for the OPERATIONS / SUBOPERATIONS review table.

The review table's row is an OPERATION (one entity — every language it writes),
and expanding it shows the SUBOPERATIONS the operation is made of: one per
property value (per language) for an update/create, one per link for a
relationship batch. This module owns the row contracts and the derivations the
page needs, with no NiceGUI import, so all of it is unit-testable.

The rules are enforced here, once:

* a suboperation is independently checkable only when a Run can execute it
  ALONE (a property value, a link). A delete/publish/merge part is not: its
  write is the whole operation, so it is listed with a lock and the reason;
* the operation's own state is DERIVED from its parts (all applied → applied, all
  discarded → discarded, anything else → pending), so the two levels can never
  contradict each other;
* the LANGUAGE belongs to the SUBOPERATION: an entity appears once in the
  table, and a translation task shows one part per language.
"""

from __future__ import annotations

from typing import Any, Iterable

from uwazi_admin_agent.configuration import DUMMY_LANGUAGE
from uwazi_admin_agent.domain.metadata_diff import render_property_value
from uwazi_admin_agent.domain.relationship_values import (
    RELATIONSHIP_TYPE,
    SELECT_TYPE,
    embedded_titles,
    relationship_selection,
    render_relationship,
)
from uwazi_admin_agent.domain.select_values import MULTISELECT_TYPE, effective_selection, labels_of
from uwazi_admin_agent.domain.suggestion import (
    CREATE_OPS,
    DISPLAY_ONLY_OPS,
    INFO_OP,
    MATCHED_OP,
    MERGE_OPS,
    MOVE_OP,
    REVERT_RECREATES_OPS,
    SKIPPED_OP,
    OperationReviewRow,
    Suggestion,
    SuggestionStatus,
    skip_reason_label,
)
from uwazi_admin_agent.drivers.web.review_view import (
    CONTEXT_CELL_CHARS,
    MATCH_COLORS,
    OP_COLORS,
    OPENABLE_OPS,
    RUN_OP_PHRASES,
    RUN_STATUS_COLORS,
    STATUS_COLORS,
    TITLE_CELL_CHARS,
    match_label,
    modified_label,
    property_heading,
    truncate,
)

# The expand column holds the chevron; the Status column carries the badge AND
# the checked/checkable counter next to it, so it needs more room than the
# review table's own status column.
EXPAND_COLUMN: str = "width: 44px"
STATUS_WITH_COUNT_COLUMN: str = "width: 152px"

# The color of the border enclosing one operation's suboperations. The op's
# Quasar color is a NAME ("primary"), not a CSS value, so the enclosure needs a
# real color per op — and the ops need colors that tell each other apart,
# because the enclosure is what tells the groups apart once all are expanded.
OP_BORDER_COLORS: dict[str, str] = {
    "update": "#2c3e50",
    "create": "#18bc9c",
    "delete": "#e53935",
    "publish": "#43a047",
    "unpublish": "#fb8c00",
    "set_publish_status": "#f9a825",
    "create_relationships": "#8e24aa",
    MOVE_OP: "#00897b",
    "update_and_delete": "#5e35b1",
    "create_and_delete": "#3949ab",
    "info": "#546e7a",
    "matched": "#2e7d32",
    "skipped": "#9e9e9e",
    "validate": "#8e24aa",
}
DEFAULT_BORDER_COLOR: str = "#607d8b"

# Operations a Run CANNOT execute one part at a time: their recorded rows are the
# whole action (a delete is Uwazi-side one call; publishing is entity-wide; a
# merge writes the target and deletes its sources in the same pass). Their
# suboperations render locked, with the reason as the lock's tooltip.
LOCKED_SUB_OPS: dict[str, str] = {
    "delete": "deleting the entity is one action — select the operation",
    "publish": "publishing is one action for the whole entity — select the operation",
    "unpublish": "unpublishing is one action for the whole entity — select the operation",
    "set_publish_status": "the publish state is one action for the whole entity — select the operation",
    MOVE_OP: "adding files is one action — select the operation",
    "update_and_delete": "a merge writes the target and deletes its sources in one Run — select the operation",
    "create_and_delete": "a merge creates the target and deletes its sources in one Run — select the operation",
}

# Suboperation status labels. The store's lifecycle IS the operator's
# vocabulary, so each state has exactly one word:
#   pending   — still to be written.
#   applied   — written to Uwazi.
#   reverted  — its write was undone, so it can be run again.
#   discarded — the operator refused it (Restore exists), so it is not runnable.
SUB_STATUS_LABELS: dict[str, str] = {
    SuggestionStatus.PENDING.value: "pending",
    SuggestionStatus.APPLIED.value: "applied",
    SuggestionStatus.REVERTED.value: "reverted",
    SuggestionStatus.DISCARDED.value: "discarded",
}

SUB_STATUS_COLORS: dict[str, str] = {
    **STATUS_COLORS,
    "applied": RUN_STATUS_COLORS["applied"],
}

# The suboperation states a Run may write again. ``applied`` is already in
# Uwazi and ``discarded`` was refused (Restore is its undo) — neither may enter
# a selection, or the Run/Discard counters would include parts the action
# cannot write.
_RUNNABLE_SUB_STATUSES: frozenset[str] = frozenset({"pending", "reverted"})


def is_runnable_status(status_label: str) -> bool:
    """Whether a suboperation in this display state can be written by a Run (pure)."""
    return status_label in _RUNNABLE_SUB_STATUSES


def non_runnable_note(status_label: str) -> str:
    """Why a suboperation cannot be selected, when it is not locked by its op (pure)."""
    return {
        "applied": "already applied — revert it first",
        "discarded": "discarded — restore it first",
    }.get(status_label, "not independently runnable")


def rerun_blocked_reason(op: str) -> str:
    """Why a reverted row of ``op`` cannot be run again (empty when it can) (pure).

    A delete's undo is a re-create and a merge's undo re-creates the sources it
    absorbed, both with FRESH sharedIds. The recorded row still names the old
    entities, so executing it again would write to an id that no longer exists
    (or, for a merge, copy from dead sources). Re-pointing the row automatically
    is not safe: the old→new mapping is per entity and the related row values
    would need the same remap. The row therefore stays visible and reverted, but
    its Run is refused, with this text as the explanation. The undo itself IS
    available from the row (a reverted delete/merge is not re-runnable, but the
    reverted state is what its Revert produced); a later Sync with Uwazi
    re-reads the operation from live state and resets it to PENDING (see
    :func:`uwazi_admin_agent.domain.review_refresh.reconciled_status`).
    """
    if op == "delete":
        return (
            "the revert re-created this entity with a new sharedId; this recorded delete still points "
            "at the old id, so it cannot be run again"
        )
    if op in MERGE_OPS:
        return (
            "the revert re-created the entities this merge absorbed with new sharedIds; this recorded "
            "merge still points at the old ids, so it cannot be run again"
        )
    return ""


def rerun_blocked(op: str, status_label: str) -> bool:
    """Whether this reverted row's op can no longer be executed (pure)."""
    return status_label == "reverted" and op in REVERT_RECREATES_OPS


# Operations whose OPERATION-level undo is not an in-place restore of one live entity:
# a create's undo is a deletion, a create-merge's target exists only as a
# ``dry-created-N`` placeholder, and a file copy appends bytes no entity
# snapshot can remove. An ``update_and_delete`` merge is NOT in this set: it is
# one operation whose undo restores its target AND re-creates the sources it
# deleted, so its row offers Revert like any modified entity. A plain ``delete``
# is NOT in it either: its undo re-creates the deleted entity from its backup
# (with a fresh sharedId), and the per-entity revert performs exactly that, so
# its applied row is revertable from the table like any other write.
NON_IN_PLACE_OPS: frozenset[str] = frozenset({"create_and_delete", MOVE_OP})


def is_selectable(op: str) -> bool:
    """Whether one suboperation of ``op`` can be run on its own (pure).

    ``info``/``matched``/``skipped`` rows are display-only, so they are never
    selectable.
    """
    return op not in LOCKED_SUB_OPS and op not in DISPLAY_ONLY_OPS


def lock_note(op: str) -> str:
    """Why a suboperation of ``op`` has no checkbox (empty when it is checkable)."""
    return LOCKED_SUB_OPS.get(op, "")


def sub_status_label(status: SuggestionStatus) -> str:
    """The table's word for one suboperation's status (pure)."""
    return SUB_STATUS_LABELS.get(status.value, status.value)


def sub_status_color(status: SuggestionStatus) -> str:
    """The Quasar color of one suboperation's status badge (pure)."""
    return SUB_STATUS_COLORS.get(sub_status_label(status), "grey-7")


def sub_label(sub: Suggestion, labels: dict[str, str] | None = None) -> str:
    """The human heading of one suboperation (pure).

    A property row names its template property by its LABEL ("Document Ref")
    when the template's properties were resolved, falling back to the raw name
    (``document_ref``); a relationship row names the link's TARGET (the thing
    the operator is connecting to); a whole-entity row names its op's phrase.
    An ``info`` row renders its arbitrary text verbatim. The raw name is kept
    visible by :func:`sub_caption` so the row stays identifiable.
    """
    if sub.op == INFO_OP:
        return "" if sub.proposed_value is None else str(sub.proposed_value)
    if sub.op == SKIPPED_OP:
        # A holdout row's headline is the MEASURED property, not the skip
        # sentence: the row exists to show what the script suggested for the
        # entity the score checks, and the reason it suggested nothing belongs
        # in the caption below it.
        if sub.is_validation and sub.property_name:
            return property_heading(sub.property_name, sub.op, labels or {})
        return skip_reason_label(sub.skip_reason)
    if sub.op == MATCHED_OP:
        return property_heading(sub.property_name, sub.op, labels or {})
    if sub.op == "create_relationships":
        rel = sub.proposed_value if isinstance(sub.proposed_value, dict) else {}
        target = sub.detail_key or str(rel.get("to_entity_shared_id") or "")
        return f"→ {target}" if target else "Link"
    if sub.property_name is None and sub.op in RUN_OP_PHRASES:
        return RUN_OP_PHRASES[sub.op]
    return property_heading(sub.property_name, sub.op, labels or {})


def sub_caption(sub: Suggestion, labels: dict[str, str] | None = None) -> str:
    """The line shown under a suboperation's heading, or "" (pure).

    A PROPERTY row is named by its template label only: the internal property
    name (``document_ref``) is an implementation identifier the operator never
    needs, and printing it under the readable label was noise in every row. The
    name still travels on the payload (``property_name``) because the actions
    address rows by it.

    Two other kinds of row keep a caption, and it is evidence, not an id: a
    relationship LINK names its relation type, and a merge's structural part
    (a copied file, a deleted source) names the source entity.
    """
    if sub.op in (INFO_OP, SKIPPED_OP, MATCHED_OP):
        return ""
    if sub.op == "create_relationships":
        rel = sub.proposed_value if isinstance(sub.proposed_value, dict) else {}
        return str(rel.get("relationship_type_name") or "")
    del labels  # the property label IS the heading; the raw name is not shown
    return ""


def _evidence_text(sub: Suggestion) -> str:
    """The input text the operator judges one suggestion against (pure).

    A regular row's ``context`` is the passage the script read. An EXCLUDED row
    has no passage: its evidence is the recorded detail — the skip reason's
    supporting text (an exception message, the page examined), kept out of the
    value columns so it can never be mistaken for a suggestion.
    """
    if sub.op == SKIPPED_OP:
        detail = "" if sub.proposed_value is None else str(sub.proposed_value).strip()
        return detail[:CONTEXT_CELL_CHARS]
    return (sub.context or "")[:CONTEXT_CELL_CHARS]


def _relationship_proposed(sub: Suggestion) -> str:
    rel = sub.proposed_value if isinstance(sub.proposed_value, dict) else {}
    kind = str(rel.get("relationship_type_name") or "")
    target = sub.detail_key or str(rel.get("to_entity_shared_id") or "")
    text = f"→ {target}" if target else "link"
    return f"{text} ({kind})" if kind else text


def _clip_display(text: str, limit: int = 120) -> str:
    """Cut an already-rendered display value to one line (pure)."""
    return text if len(text) <= limit else text[:limit] + "…"


def sub_values_full(
    sub: Suggestion,
    types: dict[str, str] | None = None,
    titles: dict[str, str] | None = None,
) -> tuple[str, str]:
    """One suboperation's ``(current, proposed)`` display pair, UNtruncated (pure).

    The table cells clip the values with CSS, but a tooltip (and the collapsed
    parent's one-line recommendation) must be able to show the whole value.
    The render rules are the same ones :func:`sub_values` applies.
    """
    if sub.op == INFO_OP:
        return "—", "" if sub.proposed_value is None else str(sub.proposed_value)
    if sub.op == SKIPPED_OP:
        # An excluded entity proposes NOTHING. Its recorded detail is the
        # evidence for the exclusion (an exception message, the page text), so
        # it is shown as that evidence — never as a "suggested value", where it
        # read like a proposal the operator could run.
        #
        # A HOLDOUT row is different: it is evidence for the score, and the
        # frozen label IS what the score measures against. Showing it beside an
        # empty proposal is the difference between "the script suggested nothing
        # and the answer was X" and an opaque row the operator cannot judge.
        if sub.is_validation:
            property_type = (types or {}).get(sub.property_name or "")
            if property_type == RELATIONSHIP_TYPE:
                return render_relationship(sub.current_value, titles), "—"
            return render_property_value(sub.current_value, property_type), "—"
        return "—", "—"
    property_type = (types or {}).get(sub.property_name or "")
    if sub.op == MATCHED_OP:
        if property_type == RELATIONSHIP_TYPE:
            return (
                render_relationship(sub.current_value, titles),
                render_relationship(sub.proposed_value, titles),
            )
        return render_property_value(sub.current_value, property_type), render_property_value(
            sub.proposed_value, property_type
        )
    if sub.op == "create_relationships":
        return "—", _relationship_proposed(sub)
    if sub.op == "set_publish_status":
        return "—", "publish" if sub.proposed_value else "unpublish"
    if sub.op == "delete":
        # A delete has no proposed PROPERTY value: the "Change" column already
        # carries the word "Delete", so repeating it here says nothing.
        return "—", "—"
    if sub.op in ("publish", "unpublish"):
        return "—", RUN_OP_PHRASES.get(sub.op, sub.op)
    if sub.op == MOVE_OP:
        return "—", _move_files_summary(sub)
    if property_type == RELATIONSHIP_TYPE:
        return (
            render_relationship(sub.current_value, titles),
            render_relationship(sub.proposed_value, titles),
        )
    return render_property_value(sub.current_value, property_type), render_property_value(sub.proposed_value, property_type)


def sub_values(
    sub: Suggestion,
    types: dict[str, str] | None = None,
    titles: dict[str, str] | None = None,
) -> tuple[str, str]:
    """One suboperation's ``(current, proposed)`` display pair (pure).

    The values are Uwazi's stored shapes rendered for a one-line cell — the
    same rendering the entity window uses. A whole-entity op has no current
    value to show (publishing/deleting is not a property), so it reads "—".
    ``types`` maps property names to their template type so a date property
    renders as a calendar date instead of its raw epoch timestamp; ``titles``
    maps related-entity sharedIds to their display titles, so a relationship
    value reads as the target entities' titles instead of bare ids.
    """
    current, proposed = sub_values_full(sub, types, titles)
    return _clip_display(current), _clip_display(proposed)


def _move_files_summary(sub: Suggestion) -> str:
    """What a standalone file copy adds to its target (pure)."""
    names = [
        str(file.get("originalname") or file.get("filename") or "") for file in sub.merge_files if isinstance(file, dict)
    ]
    names = [name for name in names if name]
    if names:
        return truncate(", ".join(names), 120)
    sources = [source for source in sub.merge_source_ids if source]
    return truncate("Files from " + ", ".join(sources), 120) if sources else "Files"


def sub_match(sub: Suggestion) -> str:
    """One suboperation's match verdict label (pure).

    Only a property value has something to compare: a whole-entity row (delete /
    publish / link) has no current counterpart, so it reads ``n/a`` rather than
    the "new" a NULL verdict means for a property.
    """
    if sub.property_name is None:
        return "n/a"
    return match_label(sub.match)


def sub_revertable(sub: Suggestion, *, in_place: bool) -> bool:
    """Whether ONE suboperation offers its own Revert (pure).

    Only a WRITTEN property row of an entity the run merely modified: that is
    the one case where putting a single key back is a complete undo. A link's
    undo is not recorded per link, and a created/deleted entity's undo is the
    operation/run-level restore. A MERGE's parts are excluded by the caller
    (``in_place=False`` for a merge operation): one target property is not the
    undo of an operation that also deleted its sources — that is the operation
    row's Revert.
    """
    return (
        in_place
        and sub.status is SuggestionStatus.APPLIED
        and sub.property_name is not None
        and sub.op in ("update", "update_and_delete")
    )


def suboperation_row(
    sub: Suggestion,
    *,
    checked: bool,
    in_place: bool,
    labels: dict[str, str] | None = None,
    types: dict[str, str] | None = None,
    titles: dict[str, str] | None = None,
) -> dict[str, Any]:
    """One suboperation's table payload (pure). Colors are resolved here, not in Vue.

    ``titles`` maps related-entity sharedIds to titles so a relationship row
    reads as the target entities — the same map the entity window uses.
    """
    status_label = sub_status_label(sub.status)
    status_color = sub_status_color(sub.status)
    if sub.is_validation:
        # Holdout evidence is neither work nor a result of a write: its badge
        # is the frozen-label comparison the score uses. "validate" already
        # appears in the Operation column, so repeating it here hid the one
        # fact the operator needs — whether the script's suggestion agrees
        # with the held-out label.
        matched = sub.match is True
        status_label = "match" if matched else "mismatch"
        status_color = MATCH_COLORS["exact" if matched else "mismatch"]
    elif sub.op == MATCHED_OP:
        status_label, status_color = "matched", "green-9"
    elif sub.op == SKIPPED_OP:
        status_label, status_color = "excluded", "grey-7"
    current, proposed = sub_values(sub, types, titles)
    current_full, proposed_full = sub_values_full(sub, types, titles)
    match = sub_match(sub)
    blocked = rerun_blocked(sub.op, status_label)
    # A validation row is NEVER runnable and NEVER selectable: its Run would write
    # a held-out entity and destroy the very measurement it exists for.
    runnable = not sub.is_validation and is_runnable_status(status_label) and not blocked and sub.op not in DISPLAY_ONLY_OPS
    # A WRITTEN row is still a selection target: the top Revert acts on the
    # entities the operator checks, and an applied row was the one thing that
    # could not be checked. ``in_place`` is the operation-level undo rule (a
    # create-merge reverts at run level, a plain update/create/delete row
    # reverts here), so an applied part of such an operation enters the
    # selection for REVERT only — never for Run or Discard.
    revert_selectable = (
        in_place and not sub.is_validation and sub.status is SuggestionStatus.APPLIED and sub.op not in DISPLAY_ONLY_OPS
    )
    run_selectable = is_selectable(sub.op) and runnable
    selectable = run_selectable or revert_selectable
    if blocked:
        note = rerun_blocked_reason(sub.op)
    elif sub.is_validation:
        note = "validation row — evidence for the score; promote the entity to run it"
    elif revert_selectable and selectable:
        note = "already applied — select it to revert this write"
    elif revert_selectable:
        # A LOCKED part (a delete's whole-entity write) has no checkbox: its
        # applied state is undone from the OPERATION row's Revert, which the
        # operation's own tick selects.
        note = "already applied — select the operation to revert it"
    else:
        note = lock_note(sub.op) if not is_selectable(sub.op) else non_runnable_note(status_label)
    property_type = (types or {}).get(sub.property_name or "")
    # A multiselect and a relationship are both value SETS: one chip per
    # proposed value, marked accepted when the operator's selection keeps it.
    # A relationship chip is the target entity's title, so the table never asks
    # the operator to read bare sharedIds.
    select_type = (
        "relationship"
        if property_type == RELATIONSHIP_TYPE
        else "select"
        if property_type == SELECT_TYPE
        else "multiselect"
        if property_type == MULTISELECT_TYPE
        else None
    )
    if select_type == "relationship":
        selected = relationship_selection(sub.proposed_value, sub.rejected_values, sub.selected_values)
        chosen = set(selected)
        proposed_ids = relationship_selection(sub.proposed_value, [], None)
        # The PROPOSAL carries bare sharedIds; the entity's CURRENT value carries
        # the titles Uwazi read (``{"value": id, "label": title}``). Merging them
        # keeps a chip readable even when the page's title lookup is unavailable —
        # otherwise the operator reads "id-1" beside a "Japan" current value.
        chip_titles = dict(embedded_titles(sub.current_value))
        chip_titles.update({key: title for key, title in (titles or {}).items() if title})
        decisions = [
            {"label": render_relationship([shared_id], chip_titles), "accepted": shared_id in chosen}
            for shared_id in proposed_ids
        ]
        # Only values the operator ADDED have no chip (the chips are the
        # proposal, marked accepted/dropped), so those are the one case the
        # cell still needs a line of its own.
        extra_selection = ", ".join(
            render_relationship([shared_id], chip_titles) for shared_id in selected if shared_id not in set(proposed_ids)
        )
    else:
        selected = effective_selection(sub.proposed_value, sub.rejected_values, sub.selected_values) if select_type else []
        # One chip per DISTINCT proposed label; accepted == the operator's own
        # selection, which may add catalog values and drop proposed ones.
        proposal_labels: list[str] = []
        if select_type:
            for label in labels_of(sub.proposed_value):
                if label not in proposal_labels:
                    proposal_labels.append(label)
        decisions = [{"label": label, "accepted": label in selected} for label in proposal_labels]
        extra_selection = ", ".join(label for label in selected if label not in set(proposal_labels))
    return {
        "id": sub.id,
        "key": f"sub-{sub.id}",
        "label": sub_label(sub, labels),
        "caption": sub_caption(sub, labels),
        "context": _evidence_text(sub),
        "name": sub.property_name or "",
        "language": sub.language or "",
        "lang": sub.language,
        "current": current,
        "proposed": proposed,
        "current_full": current_full,
        "proposed_full": proposed_full,
        "match": match,
        "match_color": MATCH_COLORS.get(match, "grey-4"),
        "status": status_label,
        "is_validation": bool(sub.is_validation),
        "status_color": status_color,
        # A part is selectable only while a Run can still write it. APPLIED and
        # DISCARDED parts stay visible (and revertable/restorable) but must not
        # inflate Run (N) / Discard (N). A REVERTED delete/merge is visible too,
        # but its old sharedId cannot be written again.
        "runnable": runnable,
        # Why the part can be selected: Run/Discard read ``run_selectable``,
        # Revert reads ``revert_selectable``, and the checkbox is offered for
        # either (the action buttons carry the counts that apply).
        "run_selectable": run_selectable,
        "revert_selectable": revert_selectable,
        "rerun_blocked": blocked,
        "rerun_blocked_reason": rerun_blocked_reason(sub.op) if blocked else "",
        "selectable": selectable,
        # Whether the part's op can be acted on alone (a property/link). The
        # status decides whether it is RUNNABLE now; this decides whether it is
        # ever addressed by its own checkbox/Run/Discard/Restore.
        "independent": is_selectable(sub.op),
        "checked": bool(checked) and selectable,
        "note": note,
        "revertable": sub_revertable(sub, in_place=in_place),
        "shared_id": sub.shared_id,
        "op": sub.op,
        "property_name": sub.property_name,
        "is_info": sub.op == INFO_OP,
        "is_display_only": sub.op in DISPLAY_ONLY_OPS,
        "structural": False,
        # Multiselect value-level decisions ([] for every other row, so
        # existing payload consumers are unaffected).
        "values": decisions,
        "has_rejected": any(not decision["accepted"] for decision in decisions),
        # A value the operator ADDED (from the catalog) has no chip of its own;
        # the cell shows it on one line. When the proposal is what will be
        # written, this is empty and nothing repeats the chips.
        "extra_selection": extra_selection,
        "select_type": select_type,
    }


# One row per recorded FILE carries the singular: each file is its own
# suboperation, so a newly attached file is a new row (never a name appended to
# a shared line that the operator can miss or that a 120-char cell truncates).
MERGE_FILE_ROW_LABELS: dict[str, str] = {
    "document": "Primary document",
    "attachment": "Supporting file",
}


def _structural_row(
    *,
    row_id: int | None,
    key: str,
    label: str,
    caption: str,
    current: str,
    proposed: str,
    status: str,
    status_color: str,
    op: str,
    shared_id: str,
) -> dict[str, Any]:
    """One display-only structural suboperation (a file copy or a source delete) (pure).

    These parts are NOT checkbox rows: the operation's own row is what Run
    executes (the file copy and, for a merge, its source deletes are one
    action). They carry the underlying suggestion's ``id`` so the operation's
    Run/Discard/Restore still reach it, but they are never selectable
    individually, and the table hides their Edit action.
    """
    blocked = rerun_blocked(op, status)
    return {
        "id": row_id,
        "key": key,
        "label": label,
        "caption": caption,
        "context": "",
        "name": "",
        "language": "",
        "lang": None,
        "current": current,
        "proposed": proposed,
        "match": "n/a",
        "match_color": MATCH_COLORS.get("n/a", "grey-4"),
        "status": status,
        "status_color": status_color,
        "runnable": is_runnable_status(status) and not blocked,
        "rerun_blocked": blocked,
        "rerun_blocked_reason": rerun_blocked_reason(op) if blocked else "",
        "selectable": False,
        # A structural part is never addressed on its own: its operation's row
        # is the thing a Run/Revert acts on.
        "run_selectable": False,
        "revert_selectable": False,
        "independent": False,
        "checked": False,
        "note": rerun_blocked_reason(op) if blocked else (lock_note(op) or "part of the operation — select the operation"),
        "revertable": False,
        "shared_id": shared_id,
        "op": op,
        "property_name": None,
        "is_info": False,
        "structural": True,
    }


def is_merge_anchor(sub: Suggestion) -> bool:
    """Whether a row is the propertyless anchor of an all-no-op merge (pure).

    When every target property a merge would write already holds the proposed
    value, the pass keeps ONE such anchor per target so the source deletes and
    the file move still have a row to hang from. It writes nothing, so no review
    surface should render it as a property change.
    """
    return sub.op in MERGE_OPS and sub.property_name is None and sub.proposed_value is None


def is_file_move_row(sub: Suggestion) -> bool:
    """Whether a row is a standalone file copy (``move_files``) (pure).

    Its file list is shown as structural rows, so rendering the row itself
    would only repeat the same files.
    """
    return sub.op == MOVE_OP


def _sources_of(primary: Suggestion) -> list[str]:
    """The distinct, non-empty sources a merge/move row carries (pure)."""
    sources: list[str] = []
    for source in primary.merge_source_ids:
        if source and source not in sources:
            sources.append(source)
    return sources


def _file_structural_rows(
    primary: Suggestion,
    *,
    status: str,
    status_color: str,
    key_prefix: str = "",
) -> list[dict[str, Any]]:
    """The file-copy parts of one merge/move row, ONE ROW PER FILE (pure).

    The plan's own ``merge_files`` is what the Run copies, so the table shows it
    file by file: attaching a new supporting file to a source makes a new
    suboperation appear after the next table refresh, and a file cannot hide
    behind another one's name (the cell holds one file, never a joined list that
    a long first name would truncate).

    A source whose recorded file list is empty (legacy rows, or a dry run
    without a repository) still gets one fallback "Uploaded files" row, so the
    operation never claims fewer copies than it will make.
    """
    sources = _sources_of(primary)
    if not sources:
        return []
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    sources_with_files: set[str] = set()
    for index, file in enumerate(primary.merge_files):
        source = str(file.get("from_shared_id") or "")
        if source not in sources:
            continue
        kind = str(file.get("kind") or "file")
        name = str(file.get("originalname") or file.get("filename") or "")
        # The file id is what Uwazi mints per upload; a replaced file gets a new
        # one, so its row is new (truthful) rather than silently retitled. A
        # legacy snapshot without ids falls back to the name and the position,
        # so two same-named files still get a row each.
        file_id = str(file.get("file_id") or "")
        identity = (source, kind, file_id or f"#{index}")
        if not (file_id or name) or identity in seen:
            continue
        seen.add(identity)
        sources_with_files.add(source)
        rows.append(
            _structural_row(
                row_id=primary.id,
                key=f"{key_prefix}file-{primary.op}-{source}-{kind}-{file_id or index}",
                label=MERGE_FILE_ROW_LABELS.get(kind, "File"),
                caption=source,
                current="—",
                proposed=name or "Copied onto the target",
                status=status,
                status_color=status_color,
                op=primary.op,
                shared_id=primary.shared_id,
            )
        )

    for source in sources:
        if source in sources_with_files:
            continue
        rows.append(
            _structural_row(
                row_id=primary.id,
                key=f"{key_prefix}files-{primary.op}-{source}",
                label="Uploaded files",
                caption=source,
                current="—",
                proposed="Copied onto the target",
                status=status,
                status_color=status_color,
                op=primary.op,
                shared_id=primary.shared_id,
            )
        )
    return rows


def merge_detail_rows(
    subs: Iterable[Suggestion],
    *,
    status: str,
    status_color: str,
) -> list[dict[str, Any]]:
    """The merge's file-copy and source-delete suboperations (pure).

    A merge row would otherwise read only as its target's property updates: the
    ``update_and_delete`` op promises a delete and a file copy, but neither had
    a part of its own. This derives both from the merge row's recorded
    ``merge_source_ids`` / ``merge_files``:

    - the file-copy parts (see :func:`_file_structural_rows`);
    - one "Delete source entity" row per source.

    The rows are display-only: the Run executes the whole merge (its target
    write when there is one, the file move, the source deletes) from the merge
    row itself.
    """
    merge_rows = [sub for sub in subs if sub.op in MERGE_OPS]
    primary = next((row for row in merge_rows if row.merge_source_ids), None)
    if primary is None:
        return []
    sources = _sources_of(primary)
    if not sources:
        return []
    rows = _file_structural_rows(primary, status=status, status_color=status_color)
    for source in sources:
        rows.append(
            _structural_row(
                row_id=primary.id,
                key=f"merge-delete-{source}",
                label="Delete source entity",
                caption=source,
                current="—",
                proposed="Delete",
                status=status,
                status_color=status_color,
                op=primary.op,
                shared_id=primary.shared_id,
            )
        )
    return rows


def file_move_detail_rows(subs: Iterable[Suggestion]) -> list[dict[str, Any]]:
    """The file-copy suboperations of standalone ``move_files`` rows (pure).

    A file copy adds the sources' uploaded files to the target and keeps the
    sources untouched, so it has file parts but NO delete part. The sources are
    named exactly like a merge's, from each row's ``merge_source_ids``.

    One row is rendered per recorded move (and carries THAT row's ``id``):
    moving two source sets to the same target is two fine-grained moves, and
    giving both structural rows the same id would let a row-level Run reach only
    one of them.
    """
    rows: list[dict[str, Any]] = []
    for primary in subs:
        if primary.op != MOVE_OP or not primary.merge_source_ids:
            continue
        status = sub_status_label(primary.status)
        rows.extend(
            _file_structural_rows(
                primary,
                status=status,
                status_color=RUN_STATUS_COLORS.get(status, "grey-7"),
                key_prefix=f"move-{primary.id or primary.shared_id}-",
            )
        )
    return rows


def operation_status(subs: Iterable[dict[str, Any]]) -> str:
    """The operation's own state, DERIVED from its parts (pure).

    ``applied`` once every part is written, ``reverted`` once every part's write
    was undone (runnable again), ``discarded`` once every part is refused,
    ``mixed`` when the parts disagree (some written, some still pending), and
    ``pending`` when nothing has happened yet. The words are the parts' own, so
    the two levels cannot contradict each other — and ``mixed`` is explicit: a
    row whose subrows span states used to read "pending" even with half of it
    already in Uwazi, which hid work that landed.

    A validation operation's parts carry the frozen-label verdicts
    (``match``/``mismatch``) instead of a lifecycle state, so those words are
    carried through the same rule: every part matching reads ``match``, every
    part differing reads ``mismatch``, and a mix reads ``mismatch`` (some value
    the score measures is wrong, which is what the operator must see).
    """
    states = {sub["status"] for sub in subs}
    if not states:
        return "pending"
    if states <= {"match", "mismatch"}:
        return "match" if states == {"match"} else "mismatch"
    if len(states) == 1:
        return states.pop() if next(iter(states)) in ("applied", "reverted", "discarded") else "pending"
    if states == {"applied", "reverted"}:
        # Every part has been written at some point: the run still holds
        # applied writes, so "mixed" is the honest word.
        return "mixed"
    return "mixed" if "applied" in states else "pending"


def status_repeats_operation(ops: Iterable[str], status: str, *, display_only: bool = False) -> bool:
    """Whether the Status badge would only repeat the operation names (pure).

    A display-only row (``matched`` / ``skipped``) has no workflow state of its
    own: its "status" IS the outcome the operation column already prints, so a
    second badge saying the same word is noise. The same holds when every
    operation of the row reads exactly like the derived status (a single
    ``delete`` row whose only part is deleted, say).
    """
    words = {"excluded" if op == SKIPPED_OP else op for op in ops}
    if not words:
        return False
    if display_only:
        return words == {status}
    return words == {status}


def operation_status_breakdown(subs: Iterable[dict[str, Any]]) -> str:
    """The parent badge's sub-line: how the parts split across states (pure).

    Empty when every part carries the same state (the badge already says it).
    Otherwise one ``N state`` fragment per state, in first-appearance order, so
    the operator can tell WHICH status the main row summarizes.
    """
    states = [sub["status"] for sub in subs]
    ordered: list[str] = []
    for state in states:
        if state not in ordered:
            ordered.append(state)
    if len(ordered) < 2:
        return ""
    counts = {state: states.count(state) for state in ordered}
    return " · ".join(f"{counts[state]} {state}" for state in ordered)


def validated_counts(subs: Iterable[dict[str, Any]]) -> tuple[int, int]:
    """``(checked, checkable)`` for one operation's suboperations (pure).

    ``(0, 0)`` means the operation has nothing to select one by one (a merge, a
    delete, a publish), rendered as "n/a" rather than "0/0".
    """
    checkable = [sub for sub in subs if sub.get("selectable")]
    return sum(1 for sub in checkable if sub.get("checked")), len(checkable)


def validated_cell(subs: Iterable[dict[str, Any]]) -> tuple[str, str]:
    """The counter shown NEXT TO the status badge: ``(text, color)`` (pure)."""
    checked, total = validated_counts(subs)
    if total == 0:
        return "n/a", "grey-4"
    if checked == total:
        return f"{checked}/{total}", "green"
    if checked:
        return f"{checked}/{total}", "orange"
    return f"{checked}/{total}", "grey-6"


def recommendation_text(subs: Iterable[dict[str, Any]]) -> str:
    """The collapsed row's one-line RECOMMENDATION, derived from its parts (pure).

    The parent row has no proposed value of its own: the suggestion belongs to
    the property child rows. The most substantial proposed value is what the
    operator wants to read on the collapsed entity (a title is short, the
    extracted text is what needs judging), so the longest non-structural value
    wins. Property rows are named by their label when nothing was proposed
    (a delete/publish row), so the cell still says what the operation does.
    """
    candidates: list[str] = []
    fallback = ""
    for sub in subs:
        if not isinstance(sub, dict):
            continue
        label = str(sub.get("label") or "").strip()
        if not fallback and label:
            fallback = label
        if sub.get("structural"):
            continue
        text = str(sub.get("proposed_full") or sub.get("proposed") or "").strip()
        if text and text != "—":
            candidates.append(text)
    if not candidates:
        return fallback
    return max(candidates, key=len)


def _operation_words(item: OperationReviewRow) -> str:
    """The Operation column's text for one review row (pure).

    A run's holdout rows are evidence, not work, so they read ``validate``
    instead of repeating the write they record. An ``update`` that the run
    EXCLUDED reads ``excluded``. A mixed operation (some holdout, some regular)
    keeps its real op names: it is work with evidence beside it.
    """
    if item.validation and item.validation >= len(item.subs) > 0:
        return "validate"
    return ", ".join("excluded" if op == SKIPPED_OP else op for op in item.ops)


def _operation_colors(item: OperationReviewRow) -> list[str]:
    """The Operation badge colors matching :func:`_operation_words` (pure)."""
    if item.validation and item.validation >= len(item.subs) > 0:
        return [OP_COLORS["validate"]]
    return [OP_COLORS.get(op, "grey-7") for op in item.ops]


def operation_open_url(
    shared_id: str,
    ops: list[str],
    languages: list[str],
    uwazi_url: str | None,
    *,
    created_shared_id: str | None = None,
) -> str | None:
    """The instance's public page for the entity, or None when it has none (pure).

    A create row holds a ``dry-created`` placeholder until its Run mints the
    real entity; ``created_shared_id`` is that minted id, so the row can link to
    the entity it actually created. Without it (not run yet, or its create was
    refused) there is nothing in Uwazi to open. A publish/relink row is not
    about the entity's current content either. The language segment prefers the
    operation's first written language, else the instance default.
    """
    if not uwazi_url:
        return None
    target = created_shared_id or shared_id
    if not created_shared_id and not OPENABLE_OPS.intersection(ops):
        return None
    if created_shared_id and not any(op in CREATE_OPS for op in ops):
        return None
    language = languages[0] if languages else DUMMY_LANGUAGE
    return f"{uwazi_url.rstrip('/')}/{language}/entity/{target}"


def is_display_only_operation(item: OperationReviewRow) -> bool:
    """Whether an operation is read-only evidence rather than runnable work (pure).

    ``info``, ``matched`` and ``skipped`` name outcomes, not writes. The
    review table must render them without a checkbox, Run, Discard, Revert or
    Edit action, and the action paths must never select them.
    """
    return bool(item.ops) and all(op in DISPLAY_ONLY_OPS for op in item.ops)


def operation_row(
    item: OperationReviewRow,
    *,
    checked_ids: Iterable[int] = (),
    uwazi_url: str | None = None,
    labels: dict[str, str] | None = None,
    types: dict[str, str] | None = None,
    titles: dict[str, str] | None = None,
) -> dict[str, Any]:
    """One operation's table payload, with its suboperations (pure).

    ``checked_ids`` is the page's SUBOPERATION selection: the payload carries it
    per part so the expanded rows render their checkboxes, and the stage's
    checked/checkable counter counts only the parts still in view (the filter
    already narrowed ``item.subs``). ``labels`` maps property names to the
    template's human labels, so a child row reads "Document Ref", not the raw
    ``document_ref``.

    Display-only operations (``info``/``matched``/``skipped``) carry no
    checkbox and no actions. ``info`` shows its headline as the target text;
    ``matched`` and ``skipped`` behave like entity rows (title, sharedId and
    Uwazi link) but read their values from the suboperations.
    """
    checked = set(checked_ids)
    is_info = bool(item.ops) and all(op == INFO_OP for op in item.ops)
    # A holdout operation is evidence whatever op its rows record: an `update`
    # the evaluation pass produced is still NOT work, so it must render without
    # a checkbox, a Run, a Discard or a Revert. `is_display_only_operation` only
    # looks at the op names, and a validation row's op is a real write op.
    all_holdout = bool(item.subs) and item.validation >= len(item.subs) and bool(item.ops)
    is_display_only = is_display_only_operation(item) or all_holdout
    in_place = not any(op in NON_IN_PLACE_OPS for op in item.ops)
    # A merge's own row is revertable (the operation restores its target and
    # re-creates the sources it deleted), but its PARTS are not: putting one
    # target property back, or undoing one copied file, is not a complete undo.
    sub_in_place = in_place and not any(op in MERGE_OPS for op in item.ops)
    if is_info:
        # The parent row carries the headline; only its detail lines are
        # rendered as suboperations. The parent itself is not a change, so it
        # never needs a checkbox, a status badge, or an action.
        submodels = [sub for sub in item.subs if sub.detail_key is not None]
    else:
        submodels = list(item.subs)
    rendered = [
        (
            sub,
            suboperation_row(
                sub,
                checked=sub.id in checked,
                in_place=sub_in_place,
                labels=labels,
                types=types,
                titles=titles,
            ),
        )
        for sub in submodels
    ]
    # The all-no-op merge anchor and a standalone file-copy row write no
    # property value of their own: their structural file/delete parts carry the
    # meaning. Their status still counts toward the operation's own state, then
    # they are dropped from the display.
    status = operation_status([payload for _sub, payload in rendered])
    subs = [payload for sub, payload in rendered if not is_merge_anchor(sub) and not is_file_move_row(sub)]
    text, color = validated_cell(subs)
    # An operation whose every part is holdout evidence keeps its derived verdict
    # (``match``/``mismatch``, see ``operation_status``) so the Status cell answers
    # "did the script get the held-out value right?" — the one question these rows
    # exist for. The Operation column already says "validate" (``_operation_words``),
    # so the verdict is the only thing the badge can add.
    if not is_display_only:
        subs.extend(merge_detail_rows(item.subs, status=status, status_color=RUN_STATUS_COLORS.get(status, "grey-7")))
        subs.extend(file_move_detail_rows(item.subs))
    blocked_sub = next((sub for sub in subs if sub.get("rerun_blocked")), None)
    rerun_blocked = blocked_sub is not None
    rerun_reason = str(blocked_sub.get("rerun_blocked_reason") or "") if blocked_sub is not None else ""
    # A create's rows carry the recorder's placeholder (``dry-created-N``)
    # until a Run mints the entity. Showing it as the "target" reads like a
    # broken id, so the Target cell shows the title the create proposes; the
    # created entity's real sharedId (once it exists) is what the row links to.
    created_shared_id = next((sub.created_shared_id for sub in item.subs if sub.created_shared_id), None)
    title = item.entity_title or item.shared_id
    if any(op in CREATE_OPS for op in item.ops) and not item.entity_title:
        # A create with no title row has no name yet: name it by what it will
        # be, never by the recorder's placeholder (which reads like a broken id).
        template = item.template_names[0] if item.template_names else ""
        title = f"New {template} entity" if template else "New entity"
    if is_info:
        # Display-only: full text (the Target cell wraps it), no link, no
        # counters, no Run/Discard/Revert/Edit action, and a dedicated status
        # word so an info row never reads as pending work.
        return {
            "row_key": item.shared_id,
            "shared_id": item.shared_id,
            "title": title,
            "title_full": title,
            "template": "—",
            "languages": [],
            "ops": INFO_OP,
            "op_colors": [OP_COLORS.get(INFO_OP, "grey-7")],
            "url": None,
            "modified": modified_label(item.modified_at),
            "rerun_blocked": False,
            "rerun_blocked_reason": "",
            "run_title": "Info rows are display-only",
            "run_status": "info",
            "run_status_color": RUN_STATUS_COLORS.get("info", "grey-7"),
            "status_hidden": True,
            "status_breakdown": "",
            "validated": "n/a",
            "validated_color": "grey-4",
            "has_validated": False,
            "has_checkable": False,
            "has_runnable": False,
            "border_color": OP_BORDER_COLORS.get(INFO_OP, DEFAULT_BORDER_COLOR),
            "in_place": False,
            "revertable": False,
            "has_applied": False,
            "subs": subs,
            "is_info": True,
            "is_display_only": True,
            "has_subs": bool(subs),
        }
    if is_display_only:
        # Matched/skipped: real entities, read-only results. The title, sharedId
        # and Uwazi link stay available; every action and selection is removed.
        #
        # A HOLDOUT row is evidence for the score, whichever op the script
        # happened to record for it: an entity the script could not process is
        # stored as ``skipped``, which used to print "excluded" — the word for
        # an entity outside the plan — on a row whose whole purpose is the
        # validation measurement the Operation filter calls ``validate``.
        if all_holdout:
            display_op = "validate"
        else:
            display_op = item.ops[0] if item.ops else ""
        display_label = "excluded" if display_op == SKIPPED_OP else display_op
        display_ops = ["validate"] if all_holdout else ["excluded" if op == SKIPPED_OP else op for op in item.ops]
        # A validation operation's Status badge is the frozen-label verdict its
        # parts carry (``match``/``mismatch``), never the word "validate" again:
        # the Operation column already says that, and the operator opened the
        # table to see whether the script got the held-out values right.
        verdict_status = status if all_holdout else display_label
        verdict_color = (
            RUN_STATUS_COLORS.get(status, "purple-7") if all_holdout else RUN_STATUS_COLORS.get(display_op, "grey-7")
        )
        return {
            "row_key": item.shared_id,
            "shared_id": item.shared_id,
            "title": truncate(title, TITLE_CELL_CHARS),
            "title_full": title,
            "template": truncate(", ".join(item.template_names) or "—", 28),
            "languages": item.languages,
            "ops": ", ".join(display_ops),
            "op_colors": [OP_COLORS.get("validate" if all_holdout else op, "grey-7") for op in item.ops],
            "url": operation_open_url(item.shared_id, item.ops, item.languages, uwazi_url),
            "modified": modified_label(item.modified_at),
            "rerun_blocked": False,
            "rerun_blocked_reason": "",
            "run_title": (
                "Validation evidence — this row is scored, never run"
                if all_holdout
                else "This row reports a result; it is display-only"
            ),
            "run_status": verdict_status,
            "run_status_color": verdict_color,
            # A matched/skipped row's status IS its operation: one badge is
            # enough, so the Status cell stays empty. A validation row is the
            # exception — its verdict is the reading the operator came for.
            "status_hidden": (
                False if all_holdout else status_repeats_operation(display_ops, display_label, display_only=True)
            ),
            "status_breakdown": operation_status_breakdown(subs) if all_holdout else "",
            "validated": "n/a",
            "validated_color": "grey-4",
            "has_validated": False,
            "has_checkable": False,
            "has_runnable": False,
            "border_color": OP_BORDER_COLORS.get(display_op, DEFAULT_BORDER_COLOR),
            "in_place": False,
            "revertable": False,
            "has_applied": False,
            "subs": subs,
            "is_info": False,
            "is_display_only": True,
            "has_subs": True,
        }
    return {
        "row_key": item.shared_id,
        "shared_id": item.shared_id,
        "title": truncate(title, TITLE_CELL_CHARS),
        "title_full": title,
        "template": truncate(", ".join(item.template_names) or "—", 28),
        "languages": item.languages,
        "ops": _operation_words(item),
        "op_colors": _operation_colors(item),
        "url": operation_open_url(
            item.shared_id,
            item.ops,
            item.languages,
            uwazi_url,
            created_shared_id=created_shared_id,
        ),
        # The id a Run minted for a create row (the Target cell's link target).
        # The sharedId is deliberately NOT rendered: the title IS the target, and
        # an identifier column beside it read as noise.
        "created_shared_id": created_shared_id,
        "modified": modified_label(item.modified_at),
        "rerun_blocked": rerun_blocked,
        "rerun_blocked_reason": rerun_reason,
        "run_title": rerun_reason or "Run this operation: the ticked suboperations (all of them when none is ticked)",
        "run_status": status,
        "run_status_color": RUN_STATUS_COLORS.get(status, "purple-7" if status == "validate" else "grey-7"),
        "status_hidden": status_repeats_operation(item.ops, status) or status == "validate",
        # How the parts split when they disagree ("" when they agree): the
        # parent badge says "mixed", this line says WHICH states it mixes.
        "status_breakdown": operation_status_breakdown(subs),
        "validated": text,
        "validated_color": color,
        "has_validated": validated_counts(subs)[1] > 0,
        "has_checkable": any(sub["selectable"] for sub in subs) and not rerun_blocked,
        "has_runnable": any(sub.get("runnable") for sub in subs) and not rerun_blocked,
        # Whether ANY part is selectable at all (Run or Revert). The row
        # checkbox is enabled on this: an applied operation has nothing to run,
        # but its write is exactly what the top Revert restores — including a
        # `delete`, whose only part is locked (no checkbox of its own) yet whose
        # undo (a re-create) is offered from the operation row. A locked part (a
        # merge's source delete) has no checkbox of its own, yet the operation
        # MUST be selectable so its Run can be triggered.
        "has_selectable": any(sub.get("selectable") or sub.get("runnable") or sub.get("revert_selectable") for sub in subs)
        and not rerun_blocked,
        "border_color": OP_BORDER_COLORS.get(item.ops[0] if item.ops else "", DEFAULT_BORDER_COLOR),
        "in_place": in_place,
        "revertable": in_place and item.applied > 0,
        "has_applied": item.applied > 0,
        "subs": subs,
        "is_info": False,
        "is_display_only": False,
        "has_subs": True,
    }


def selected_subs(rows: Iterable[dict[str, Any]]) -> list[tuple[int, str, str | None, str]]:
    """The checked suboperations of the page payloads as ``(id, sharedId, language, op)`` (pure).

    The parent row's own selection is not in the payload: reading the parts is
    what makes "the checked set" mean exactly the rows the operator ticked,
    including the locked parts an operation's checkbox selected on their behalf.
    """
    refs: list[tuple[int, str, str | None, str]] = []
    seen: set[int] = set()
    for row in rows:
        for sub in row.get("subs") or []:
            if not isinstance(sub, dict) or not sub.get("checked"):
                continue
            sub_id = sub.get("id")
            if not isinstance(sub_id, int) or sub_id in seen:
                continue
            seen.add(sub_id)
            refs.append((sub_id, str(sub.get("shared_id") or ""), sub.get("lang"), str(sub.get("op") or "")))
    return refs


def sub_refs(subs: Iterable[dict[str, Any]], *, checked_only: bool = False) -> list[tuple[int, str, str | None, str]]:
    """One operation's parts as ``(id, sharedId, language, op)`` refs (pure)."""
    refs: list[tuple[int, str, str | None, str]] = []
    for sub in subs:
        if not isinstance(sub, dict) or not isinstance(sub.get("id"), int):
            continue
        if checked_only and not sub.get("checked"):
            continue
        refs.append((int(sub["id"]), str(sub.get("shared_id") or ""), sub.get("lang"), str(sub.get("op") or "")))
    return refs


def pairs_of_refs(refs: Iterable[tuple[int, str, str | None, str]]) -> list[tuple[str, str | None]]:
    """The distinct ``(sharedId, language)`` pairs of a suboperation selection (pure).

    The apply use case addresses entity-language rows by pair; the suboperation
    ids narrow it below the pair, so a Run needs both.
    """
    pairs: list[tuple[str, str | None]] = []
    for _id, shared_id, language, _op in refs:
        if shared_id and (shared_id, language) not in pairs:
            pairs.append((shared_id, language))
    return pairs


def ids_of_refs(refs: Iterable[tuple[int, str, str | None, str]]) -> list[int]:
    """The distinct row ids of a suboperation selection (pure)."""
    ids: list[int] = []
    for ref_id, _shared_id, _language, _op in refs:
        if ref_id not in ids:
            ids.append(ref_id)
    return ids


def ops_of_refs(refs: Iterable[tuple[int, str, str | None, str]]) -> list[str]:
    """The distinct ops of a suboperation selection, first-appearance order (pure)."""
    ops: list[str] = []
    for _id, _shared_id, _language, op in refs:
        if op and op not in ops:
            ops.append(op)
    return ops


def languages_of_refs(refs: Iterable[tuple[int, str, str | None, str]]) -> list[str]:
    """The distinct languages of a selection, language-independent last (pure)."""
    languages: list[str] = []
    saw_agnostic = False
    for _id, _shared_id, language, _op in refs:
        if language is None:
            saw_agnostic = True
        elif language not in languages:
            languages.append(language)
    if saw_agnostic:
        languages.append("—")
    return languages


def entities_of_refs(refs: Iterable[tuple[int, str, str | None, str]]) -> list[str]:
    """The distinct sharedIds of a selection (one entity reverts once) (pure)."""
    entities: list[str] = []
    for _id, shared_id, _language, _op in refs:
        if shared_id and shared_id not in entities:
            entities.append(shared_id)
    return entities


__all__ = [
    "DEFAULT_BORDER_COLOR",
    "EXPAND_COLUMN",
    "LOCKED_SUB_OPS",
    "NON_IN_PLACE_OPS",
    "OP_BORDER_COLORS",
    "STATUS_WITH_COUNT_COLUMN",
    "SUB_STATUS_COLORS",
    "SUB_STATUS_LABELS",
    "entities_of_refs",
    "file_move_detail_rows",
    "ids_of_refs",
    "is_file_move_row",
    "is_display_only_operation",
    "is_merge_anchor",
    "is_runnable_status",
    "is_selectable",
    "languages_of_refs",
    "lock_note",
    "non_runnable_note",
    "merge_detail_rows",
    "operation_open_url",
    "operation_row",
    "operation_status",
    "operation_status_breakdown",
    "ops_of_refs",
    "pairs_of_refs",
    "recommendation_text",
    "rerun_blocked",
    "rerun_blocked_reason",
    "selected_subs",
    "sub_caption",
    "sub_label",
    "sub_match",
    "sub_refs",
    "sub_revertable",
    "status_repeats_operation",
    "sub_status_color",
    "sub_status_label",
    "sub_values",
    "suboperation_row",
    "validated_cell",
    "validated_counts",
]
