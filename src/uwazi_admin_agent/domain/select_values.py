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


def rejected_after_toggle(rejected: list[str], value: str, accepted: bool) -> list[str]:
    """New rejected list: adds ``value`` (once) when ``accepted`` is False,
    removes it otherwise."""
    if accepted:
        removed = [r for r in rejected if r != value]
        return removed
    if value in rejected:
        return list(rejected)
    return [*rejected, value]


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
    "labels_of",
    "rejected_after_toggle",
    "select_value_envelope",
    "unpredicted_values",
    "value_decisions",
]
