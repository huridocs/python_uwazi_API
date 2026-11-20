"""Per-value accept/reject decisions for select/multiselect proposals (pure).

A multiselect change is recorded as ONE suboperation whose ``proposed_value`` is
the list of thesaurus label strings the script would write. This module holds
the pure rules every other layer (store, apply path, review UI) uses to derive
what the operator accepted, so "accepted" is never re-derived locally.
"""

from __future__ import annotations

from typing import Any

# The discard reason a fully rejected multiselect row carries: it is NOT a real
# operator refusal, it only means "nothing left to keep", so undo/re-record
# paths may reset the row to PENDING (see store + select_values rules).
ALL_VALUES_REJECTED_REASON: str = "all predicted values rejected"

MULTISELECT_TYPE: str = "multiselect"
SELECT_TYPES: frozenset[str] = frozenset({"select", "multiselect"})


def labels_of(value: Any) -> list[str]:
    """The value labels a proposal/current multiselect value carries, in order.

    Accepts ``["A", "B"]``, ``[{"value": "uuid", "label": "A"}]``, a bare str,
    ``None``. A dict entry uses its ``"label"`` when present, else its
    ``"value"``; non-str items are ``str()``'d.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    labels: list[str] = []
    for item in value:
        if isinstance(item, dict):
            label = item.get("label", item.get("value"))
        else:
            label = item
        if label is None:
            label = ""
        labels.append(str(label))
    return labels


def accepted_values(proposal: Any, rejected: list[str]) -> list[str]:
    """``labels_of(proposal)`` minus ``rejected``; order preserved, duplicates
    collapsed (first occurrence wins) because Uwazi de-duplicates a multiselect
    by value id."""
    dropped = set(rejected)
    seen: set[str] = set()
    accepted: list[str] = []
    for label in labels_of(proposal):
        if label in dropped or label in seen:
            continue
        seen.add(label)
        accepted.append(label)
    return accepted


def all_values_rejected(proposal: Any, rejected: list[str]) -> bool:
    """True when the proposal has at least one value and none of them is
    accepted."""
    labels = labels_of(proposal)
    if not labels:
        return False
    return not accepted_values(labels, rejected)


def _dedupe(labels: list[str]) -> list[str]:
    """``labels`` with duplicates collapsed, first occurrence wins."""
    seen: set[str] = set()
    unique: list[str] = []
    for label in labels:
        if label in seen:
            continue
        seen.add(label)
        unique.append(label)
    return unique


def effective_selection(proposal: Any, rejected: list[str], selected: list[str] | None) -> list[str]:
    """The labels a multiselect row currently writes: the operator's explicit
    ``selected`` when it was set (an EMPTY list means "nothing"), otherwise the
    proposal minus ``rejected``."""
    if selected is not None:
        return _dedupe(labels_of(selected))
    return accepted_values(proposal, rejected)


def value_options(
    proposal: Any,
    rejected: list[str],
    selected: list[str] | None,
    available: list[str],
) -> list[dict[str, Any]]:
    """The ordered options of the multiselect picker, each
    ``{"label": <str>, "recommended": <bool>}``: the proposal's distinct labels
    first (recommended), then every ``available`` catalog label the proposal
    does not carry, then any selected label in neither set (so an out-of-catalog
    selection stays visible and removable). Labels are unique, first occurrence
    wins — the picker keys its options by the display string."""
    recommended = _dedupe(labels_of(proposal))
    proposed = set(recommended)
    options: list[dict[str, Any]] = [{"label": label, "recommended": True} for label in recommended]
    seen = set(recommended)
    for label in available:
        if label in seen:
            continue
        seen.add(label)
        options.append({"label": label, "recommended": False})
    for label in effective_selection(proposal, rejected, selected):
        if label in seen:
            continue
        seen.add(label)
        options.append({"label": label, "recommended": label in proposed})
    return options


def catalog_labels(ids_by_label: dict[str, str]) -> list[str]:
    """The picker option list for a thesaurus: ONE label per value id, in the
    map's own order. A grouped thesaurus maps both ``"Child"`` and
    ``"Group: Child"`` to one id; the qualified (``": "``) form is the label
    emitted, at the position of the id's first appearance."""
    order: list[str] = []
    label_by_id: dict[str, str] = {}
    for label, value_id in ids_by_label.items():
        if value_id not in label_by_id:
            order.append(value_id)
            label_by_id[value_id] = label
        elif ": " in label:
            label_by_id[value_id] = label
    return [label_by_id[value_id] for value_id in order]


def value_decisions(proposal: Any, rejected: list[str]) -> list[dict[str, Any]]:
    """``[{"label": <str>, "accepted": <bool>}]`` for the UI, one entry per
    distinct label."""
    dropped = set(rejected)
    seen: set[str] = set()
    decisions: list[dict[str, Any]] = []
    for label in labels_of(proposal):
        if label in seen:
            continue
        seen.add(label)
        decisions.append({"label": label, "accepted": label not in dropped})
    return decisions


def unpredicted_values(current: Any, proposal: Any) -> list[str]:
    """Labels present in ``current`` (live Uwazi) but absent from the proposal —
    the values the whole-list replace will drop. Order follows ``current``,
    de-duplicated."""
    proposed = set(labels_of(proposal))
    seen: set[str] = set()
    dropped: list[str] = []
    for label in labels_of(current):
        if label in proposed or label in seen:
            continue
        seen.add(label)
        dropped.append(label)
    return dropped


def select_value_envelope(
    labels: list[str],
    ids_by_label: dict[str, str],
    property_name: str,
) -> list[dict[str, str]]:
    """Uwazi's metadata shape for the accepted labels: ``[{"value": <uuid>}]``,
    ``[]`` when empty. A label found in ``ids_by_label`` maps to its uuid; a
    label that IS one of ``ids_by_label.values()`` (a proposal that already
    carries uuids) maps to itself; any other label raises
    ``UnknownThesaurusValue(property_name, label)``. Output is de-duplicated by
    uuid."""
    if not labels:
        return []
    ids_by_uuid = {v: v for v in ids_by_label.values()}
    selected_ids: list[str] = []
    seen: set[str] = set()
    for label in labels:
        thesaurus_id = ids_by_label.get(label)
        if thesaurus_id is None:
            thesaurus_id = ids_by_uuid.get(label)
        if thesaurus_id is None:
            raise UnknownThesaurusValue(property_name, label)
        if thesaurus_id in seen:
            continue
        seen.add(thesaurus_id)
        selected_ids.append(thesaurus_id)
    return [{"value": thesaurus_id} for thesaurus_id in selected_ids]


class UnknownThesaurusValue(ValueError):
    def __init__(self, property_name: str, label: str) -> None:
        super().__init__(f"thesaurus value {label!r} of property {property_name!r} does not exist on the instance")


__all__ = [
    "ALL_VALUES_REJECTED_REASON",
    "MULTISELECT_TYPE",
    "SELECT_TYPES",
    "UnknownThesaurusValue",
    "accepted_values",
    "all_values_rejected",
    "catalog_labels",
    "effective_selection",
    "labels_of",
    "select_value_envelope",
    "unpredicted_values",
    "value_decisions",
    "value_options",
]
