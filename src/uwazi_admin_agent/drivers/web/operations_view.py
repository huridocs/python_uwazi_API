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
* the operation's own state is DERIVED from its parts (all run → runned, all
  discarded → discarded, anything else → pending), so the two levels can never
  contradict each other;
* the LANGUAGE belongs to the SUBOPERATION: an entity appears once in the
  table, and a translation task shows one part per language.
"""

from __future__ import annotations

from typing import Any, Iterable

from uwazi_admin_agent.configuration import DUMMY_LANGUAGE
from uwazi_admin_agent.domain.suggestion import (
    INFO_OP,
    TITLE_PROPERTY,
    OperationReviewRow,
    Suggestion,
    SuggestionStatus,
)
from uwazi_admin_agent.drivers.web.review_view import (
    MATCH_COLORS,
    OP_COLORS,
    OPENABLE_OPS,
    RUN_OP_PHRASES,
    RUN_STATUS_COLORS,
    STATUS_COLORS,
    TITLE_CELL_CHARS,
    match_label,
    property_caption,
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
    "update_and_delete": "#5e35b1",
    "create_and_delete": "#3949ab",
    "info": "#546e7a",
}
DEFAULT_BORDER_COLOR: str = "#607d8b"

# Ops that a Run CANNOT execute one part at a time: their recorded rows are the
# whole action (a delete is Uwazi-side one call; publishing is entity-wide; a
# merge writes the target and deletes its sources in the same pass). Their
# suboperations render locked, with the reason as the lock's tooltip.
LOCKED_SUB_OPS: dict[str, str] = {
    "delete": "deleting the entity is one action — select the operation",
    "publish": "publishing is one action for the whole entity — select the operation",
    "unpublish": "unpublishing is one action for the whole entity — select the operation",
    "set_publish_status": "the publish state is one action for the whole entity — select the operation",
    "update_and_delete": "a merge writes the target and deletes its sources in one Run — select the operation",
    "create_and_delete": "a merge creates the target and deletes its sources in one Run — select the operation",
}

# Suboperation status labels. The store keeps the review lifecycle
# (pending/accepted/edited/discarded/applied/reverted); the table speaks the
# same words for every state the operator can actually see, with "applied" read
# as "runned" (written to Uwazi) — the word the operation badge uses.
SUB_STATUS_LABELS: dict[str, str] = {
    SuggestionStatus.PENDING.value: "pending",
    SuggestionStatus.ACCEPTED.value: "accepted",
    SuggestionStatus.EDITED.value: "edited",
    SuggestionStatus.APPLIED.value: "runned",
    SuggestionStatus.REVERTED.value: "reverted",
    SuggestionStatus.DISCARDED.value: "discarded",
    SuggestionStatus.OBSOLETE.value: "obsolete",
}

SUB_STATUS_COLORS: dict[str, str] = {
    **STATUS_COLORS,
    "runned": RUN_STATUS_COLORS["runned"],
}

# Ops whose undo is an in-place restore of a modified entity. A create's undo is
# a deletion, a delete's is a re-create, and a merge's touches several entities
# (the target AND its deleted sources), so none of them can be restored row by
# row — the same rule the entity table already applied.
NON_IN_PLACE_OPS: frozenset[str] = frozenset({"create", "delete", "create_and_delete", "update_and_delete"})


def is_selectable(op: str) -> bool:
    """Whether one suboperation of ``op`` can be run on its own (pure).

    ``info`` rows are display-only, so they are never selectable.
    """
    return op not in LOCKED_SUB_OPS and op != INFO_OP


def lock_note(op: str) -> str:
    """Why a suboperation of ``op`` has no checkbox (empty when it is checkable)."""
    return LOCKED_SUB_OPS.get(op, "")


def sub_status_label(status: SuggestionStatus) -> str:
    """The table's word for one suboperation's status (pure)."""
    return SUB_STATUS_LABELS.get(status.value, status.value)


def sub_status_color(status: SuggestionStatus) -> str:
    """The Quasar color of one suboperation's status badge (pure)."""
    return SUB_STATUS_COLORS.get(sub_status_label(status), "grey")


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
    if sub.op == "create_relationships":
        rel = sub.proposed_value if isinstance(sub.proposed_value, dict) else {}
        target = sub.detail_key or str(rel.get("to_entity_shared_id") or "")
        return f"→ {target}" if target else "Link"
    if sub.property_name is None and sub.op in RUN_OP_PHRASES:
        return RUN_OP_PHRASES[sub.op]
    return property_heading(sub.property_name, sub.op, labels or {})


def sub_caption(sub: Suggestion, labels: dict[str, str] | None = None) -> str:
    """The raw name shown under a suboperation's heading, or "" (pure)."""
    if sub.op == INFO_OP:
        return ""
    if sub.op == "create_relationships":
        rel = sub.proposed_value if isinstance(sub.proposed_value, dict) else {}
        return str(rel.get("relationship_type_name") or "")
    return property_caption(sub.property_name, labels or {})


def _relationship_proposed(sub: Suggestion) -> str:
    rel = sub.proposed_value if isinstance(sub.proposed_value, dict) else {}
    kind = str(rel.get("relationship_type_name") or "")
    target = sub.detail_key or str(rel.get("to_entity_shared_id") or "")
    text = f"→ {target}" if target else "link"
    return f"{text} ({kind})" if kind else text


def sub_values(sub: Suggestion) -> tuple[str, str]:
    """One suboperation's ``(current, proposed)`` display pair (pure).

    The values are Uwazi's stored shapes rendered for a one-line cell — the
    same rendering the entity window uses. A whole-entity op has no current
    value to show (publishing/deleting is not a property), so it reads "—".
    """
    if sub.op == INFO_OP:
        return "—", "" if sub.proposed_value is None else str(sub.proposed_value)
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
    return truncate(sub.current_value, 120), truncate(sub.proposed_value, 120)


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
    undo is not recorded per link, and a created/deleted/merged entity's undo is
    the operation/run-level restore.
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
) -> dict[str, Any]:
    """One suboperation's table payload (pure). Colors are resolved here, not in Vue."""
    status_label = sub_status_label(sub.status)
    current, proposed = sub_values(sub)
    match = sub_match(sub)
    selectable = is_selectable(sub.op)
    return {
        "id": sub.id,
        "label": sub_label(sub, labels),
        "caption": sub_caption(sub, labels),
        "name": sub.property_name or "",
        "language": sub.language or "",
        "lang": sub.language,
        "current": current,
        "proposed": proposed,
        "match": match,
        "match_color": MATCH_COLORS.get(match, "grey-4"),
        "status": status_label,
        "status_color": sub_status_color(sub.status),
        "selectable": selectable,
        "checked": bool(checked) and selectable,
        "note": lock_note(sub.op),
        "revertable": sub_revertable(sub, in_place=in_place),
        "shared_id": sub.shared_id,
        "op": sub.op,
        "property_name": sub.property_name,
        "is_info": sub.op == INFO_OP,
    }


def operation_status(subs: Iterable[dict[str, Any]]) -> str:
    """The operation's own state, DERIVED from its parts (pure).

    ``runned`` once every part is written, ``discarded`` once every part is
    refused, anything else ``pending`` — work remains. The words are the parts'
    own, so the two levels cannot contradict each other.
    """
    states = {sub["status"] for sub in subs}
    if states == {"runned"}:
        return "runned"
    if states == {"discarded"}:
        return "discarded"
    if states == {"obsolete"}:
        return "obsolete"
    return "pending"


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


def operation_open_url(shared_id: str, ops: list[str], languages: list[str], uwazi_url: str | None) -> str | None:
    """The instance's public page for the entity, or None when it has none (pure).

    A create row still holds its ``dry-created`` placeholder, and a
    publish/relink row is not about the entity's current content — the same
    rule the entity table's link used. The language segment prefers the
    operation's first written language, else the instance default.
    """
    if not uwazi_url or not OPENABLE_OPS.intersection(ops):
        return None
    language = languages[0] if languages else DUMMY_LANGUAGE
    return f"{uwazi_url.rstrip('/')}/{language}/entity/{shared_id}"


def operation_row(
    item: OperationReviewRow,
    *,
    checked_ids: Iterable[int] = (),
    uwazi_url: str | None = None,
    labels: dict[str, str] | None = None,
) -> dict[str, Any]:
    """One operation's table payload, with its suboperations (pure).

    ``checked_ids`` is the page's SUBOPERATION selection: the payload carries it
    per part so the expanded rows render their checkboxes, and the stage's
    checked/checkable counter counts only the parts still in view (the filter
    already narrowed ``item.subs``). ``labels`` maps property names to the
    template's human labels, so a child row reads "Document Ref", not the raw
    ``document_ref``.

    An ``info`` operation is display-only: its parent row carries the arbitrary
    text and only its detail lines become suboperations. The payload is marked
    ``is_info`` so the page hides every checkbox and action.
    """
    checked = set(checked_ids)
    is_info = INFO_OP in item.ops
    in_place = not any(op in NON_IN_PLACE_OPS for op in item.ops)
    if is_info:
        # The parent row carries the headline; only its detail lines are
        # rendered as suboperations. The parent itself is not a change, so it
        # never needs a checkbox, a status badge, or an action.
        submodels = [sub for sub in item.subs if sub.detail_key is not None]
    else:
        submodels = list(item.subs)
    subs = [suboperation_row(sub, checked=sub.id in checked, in_place=in_place, labels=labels) for sub in submodels]
    text, color = validated_cell(subs)
    status = operation_status(subs)
    title = item.entity_title or item.shared_id
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
            "op_colors": [OP_COLORS.get(INFO_OP, "grey")],
            "url": None,
            "run_status": "info",
            "run_status_color": RUN_STATUS_COLORS.get("info", "grey"),
            "validated": "n/a",
            "validated_color": "grey-4",
            "has_validated": False,
            "has_checkable": False,
            "border_color": OP_BORDER_COLORS.get(INFO_OP, DEFAULT_BORDER_COLOR),
            "in_place": False,
            "revertable": False,
            "has_applied": False,
            "subs": subs,
            "is_info": True,
            "has_subs": bool(subs),
        }
    return {
        "row_key": item.shared_id,
        "shared_id": item.shared_id,
        "title": truncate(title, TITLE_CELL_CHARS),
        "title_full": title,
        "template": truncate(", ".join(item.template_names) or "—", 28),
        "languages": item.languages,
        "ops": ", ".join(item.ops),
        "op_colors": [OP_COLORS.get(op, "grey") for op in item.ops],
        "url": operation_open_url(item.shared_id, item.ops, item.languages, uwazi_url),
        "run_status": status,
        "run_status_color": RUN_STATUS_COLORS.get(status, "grey"),
        "validated": text,
        "validated_color": color,
        "has_validated": validated_counts(subs)[1] > 0,
        "has_checkable": any(sub["selectable"] for sub in subs),
        "border_color": OP_BORDER_COLORS.get(item.ops[0] if item.ops else "", DEFAULT_BORDER_COLOR),
        "in_place": in_place,
        "revertable": in_place and item.applied > 0,
        "has_applied": item.applied > 0,
        "subs": subs,
        "is_info": False,
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


# --- accuracy ----------------------------------------------------------------


def property_accuracy(scores: dict[str, tuple[int, int]]) -> dict[str, float | None]:
    """Each property's accuracy, in percent (pure).

    Only verdicts that HAVE something to be right or wrong about count (exact
    and mismatch). A ``new`` value has no current counterpart, so a property
    with no comparable value has no score at all (None) and is left out of the
    averages rather than counted as zero.
    """
    result: dict[str, float | None] = {}
    for name, (exact, mismatch) in scores.items():
        total = exact + mismatch
        result[name] = (100.0 * exact / total) if total else None
    return result


def average_accuracy(scores: dict[str, float | None]) -> float | None:
    """The mean of the properties' accuracy scores (pure; unscored ones left out)."""
    values = [score for score in scores.values() if score is not None]
    return (sum(values) / len(values)) if values else None


def accuracy_label(score: float | None) -> str:
    """One accuracy figure as the review page writes it (pure)."""
    return "n/a" if score is None else f"{score:.1f}%"


def property_options(scores: dict[str, tuple[int, int]], labels: dict[str, str] | None = None) -> dict[str, str]:
    """The Property filter's options: every property the task writes, with its accuracy (pure).

    The value is the property's raw name (what Uwazi stores, and what the store
    filters on); the label is the human name followed by the property's
    accuracy when it has one.
    """
    accuracy = property_accuracy(scores)
    human = labels or {}
    options: dict[str, str] = {}
    for name in sorted(scores):
        label = human.get(name) or ("Title" if name == TITLE_PROPERTY else name)
        score = accuracy.get(name)
        options[name] = label if score is None else f"{label} — {accuracy_label(score)} accuracy"
    return options


def any_property_option(scores: dict[str, tuple[int, int]]) -> str:
    """The Property filter's "any" option: the average accuracy (pure).

    The mean of the properties' accuracy scores — the properties WITHOUT one
    are left out, because they are unscored rather than inaccurate. With no
    scored property at all there is no figure to show, so the option reads
    plainly ("any property").
    """
    average = average_accuracy(property_accuracy(scores))
    if average is None:
        return "any property"
    return f"any property — {accuracy_label(average)} accuracy"


__all__ = [
    "DEFAULT_BORDER_COLOR",
    "EXPAND_COLUMN",
    "LOCKED_SUB_OPS",
    "NON_IN_PLACE_OPS",
    "OP_BORDER_COLORS",
    "STATUS_WITH_COUNT_COLUMN",
    "SUB_STATUS_COLORS",
    "SUB_STATUS_LABELS",
    "accuracy_label",
    "any_property_option",
    "average_accuracy",
    "entities_of_refs",
    "ids_of_refs",
    "is_selectable",
    "languages_of_refs",
    "lock_note",
    "operation_open_url",
    "operation_row",
    "operation_status",
    "ops_of_refs",
    "pairs_of_refs",
    "property_accuracy",
    "property_options",
    "selected_subs",
    "sub_caption",
    "sub_label",
    "sub_match",
    "sub_refs",
    "sub_revertable",
    "sub_status_color",
    "sub_status_label",
    "sub_values",
    "suboperation_row",
    "validated_cell",
    "validated_counts",
]
