"""Per-value accept/reject rules for multiselect proposals (pure, offline)."""

import pytest

from uwazi_admin_agent.domain.select_values import (
    ALL_VALUES_REJECTED_REASON,
    MULTISELECT_TYPE,
    SELECT_TYPES,
    UnknownThesaurusValue,
    accepted_values,
    all_values_rejected,
    catalog_labels,
    effective_selection,
    labels_of,
    select_value_envelope,
    unpredicted_values,
    value_decisions,
    value_options,
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


def test_effective_selection_prefers_explicit_selection() -> None:
    assert effective_selection(["A", "B"], ["B"], ["C"]) == ["C"]
    # An empty explicit selection means "nothing", not "fall back to the proposal".
    assert effective_selection(["A", "B"], [], []) == []
    # No explicit selection → the proposal minus the rejections.
    assert effective_selection(["A", "B"], ["B"], None) == ["A"]


def test_effective_selection_deduplicates_selected_labels() -> None:
    assert effective_selection(["A"], [], ["C", "C", "A"]) == ["C", "A"]


def test_value_options_orders_proposal_then_catalog_then_selection() -> None:
    options = value_options(["Alpha", "Beta"], [], ["Alpha", "Beta", "Zeta"], ["Beta", "Gamma", "Alpha"])
    assert options == [
        {"label": "Alpha", "recommended": True},
        {"label": "Beta", "recommended": True},
        {"label": "Gamma", "recommended": False},
        {"label": "Zeta", "recommended": False},
    ]


def test_value_options_keeps_out_of_catalog_selection_visible() -> None:
    options = value_options(["Alpha"], [], ["Delta"], [])
    assert options == [
        {"label": "Alpha", "recommended": True},
        {"label": "Delta", "recommended": False},
    ]


def test_value_options_labels_are_unique() -> None:
    options = value_options(["Alpha", "Alpha"], [], ["Alpha"], ["Alpha"])
    assert [option["label"] for option in options] == ["Alpha"]


def test_catalog_labels_collapses_a_grouped_value_to_its_qualified_label() -> None:
    ids = {"A": "u1", "Group: B": "u2", "B": "u2", "C": "u3"}
    assert catalog_labels(ids) == ["A", "Group: B", "C"]


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
