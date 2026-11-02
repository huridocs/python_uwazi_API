"""Per-value accept/reject rules for multiselect proposals (pure, offline)."""

import pytest

from uwazi_admin_agent.domain.select_values import (
    ALL_VALUES_REJECTED_REASON,
    MULTISELECT_TYPE,
    SELECT_TYPES,
    UnknownThesaurusValue,
    accepted_values,
    all_values_rejected,
    labels_of,
    rejected_after_toggle,
    select_value_envelope,
    unpredicted_values,
    value_decisions,
)


def test_labels_of_plain_list() -> None:
    assert labels_of(["A", "B"]) == ["A", "B"]


def test_labels_of_enveloped_entries() -> None:
    assert labels_of([{"value": "u1", "label": "A"}, {"value": "u2"}]) == ["A", "u2"]


def test_labels_of_none_and_bare_str() -> None:
    assert labels_of(None) == []
    assert labels_of("A") == ["A"]


def test_accepted_values_subtracts_rejected() -> None:
    assert accepted_values(["A", "B"], ["B"]) == ["A"]


def test_accepted_values_collapses_duplicates() -> None:
    assert accepted_values(["A", "B", "A"], []) == ["A", "B"]


def test_all_values_rejected_true_only_when_everything_dropped() -> None:
    assert all_values_rejected(["A"], ["A"]) is True
    assert all_values_rejected(["A", "B"], ["A"]) is False
    assert all_values_rejected([], ["A"]) is False
    assert all_values_rejected(None, []) is False


def test_rejected_after_toggle_add_and_remove() -> None:
    assert rejected_after_toggle([], "B", accepted=False) == ["B"]
    assert rejected_after_toggle(["B"], "B", accepted=False) == ["B"]  # idempotent add
    assert rejected_after_toggle(["A", "B"], "B", accepted=True) == ["A"]
    assert rejected_after_toggle(["A"], "B", accepted=True) == ["A"]  # idempotent remove


def test_value_decisions_per_distinct_label() -> None:
    decisions = value_decisions(["A", "B", "A"], ["B"])
    assert decisions == [
        {"label": "A", "accepted": True},
        {"label": "B", "accepted": False},
    ]


def test_unpredicted_values_finds_current_only_labels() -> None:
    current = [{"value": "u1", "label": "Old"}]
    assert unpredicted_values(current, ["New"]) == ["Old"]


def test_unpredicted_values_deduplicates() -> None:
    assert unpredicted_values(["Old", "Old", "Kept"], ["Kept"]) == ["Old"]


def test_select_value_envelope_maps_labels_to_uuids() -> None:
    assert select_value_envelope(["Alpha"], {"Alpha": "uuid-1"}, "keywords") == [{"value": "uuid-1"}]


def test_select_value_envelope_empty_is_empty() -> None:
    assert select_value_envelope([], {}, "keywords") == []


def test_select_value_envelope_accepts_uuid_labels() -> None:
    # A proposal that already carries uuids (enveloped values) maps to itself.
    assert select_value_envelope(["uuid-1"], {"Alpha": "uuid-1"}, "keywords") == [{"value": "uuid-1"}]


def test_select_value_envelope_rejects_unknown_label() -> None:
    with pytest.raises(UnknownThesaurusValue) as err:
        select_value_envelope(["Omega"], {"Alpha": "uuid-1"}, "keywords")
    message = str(err.value)
    assert "Omega" in message
    assert "keywords" in message


def test_select_value_envelope_deduplicates_by_uuid() -> None:
    # The qualified "Group: Child" key and the bare child label are the SAME value.
    ids = {"Resolution": "u3", "HRC: Resolution": "u3"}
    assert select_value_envelope(["HRC: Resolution", "Resolution"], ids, "keywords") == [{"value": "u3"}]


def test_sentinel_and_types_constants() -> None:
    assert ALL_VALUES_REJECTED_REASON == "all predicted values rejected"
    assert MULTISELECT_TYPE == "multiselect"
    assert "select" in SELECT_TYPES and "multiselect" in SELECT_TYPES
