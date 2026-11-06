"""Isolated unit tests for thesaurus label→id resolution (per AGENTS.md).

Pure: a literal value tree, a label map, and literal filter values in; ids or a
``SearchError`` out. No instance, network, or stand-ins.
"""

import pytest

from uwazi_api.domain.exceptions import SearchError
from uwazi_api.domain.thesauri import Thesauri
from uwazi_api.domain.thesauri_label import label_id_map, resolve_labels_to_ids

VALUES = [
    {"label": "Resolution", "id": "id-resolution"},
    {"label": "Statement", "id": "id-statement"},
]


def _label_map() -> dict[str, str]:
    return label_id_map(Thesauri.model_validate({"_id": "th", "name": "Document Types", "values": VALUES}).values)


def test_labels_resolve_to_ids() -> None:
    assert resolve_labels_to_ids(["Resolution"], _label_map(), "Document Types", "Document Type") == ["id-resolution"]


def test_an_already_resolved_id_passes_through() -> None:
    """Re-resolving a filter (a caller paging with the same object) must not fail."""
    resolved = resolve_labels_to_ids(["Resolution"], _label_map(), "Document Types", "Document Type")
    assert resolve_labels_to_ids(resolved, _label_map(), "Document Types", "Document Type") == resolved


def test_the_missing_sentinel_is_preserved() -> None:
    """Uwazi uses ``missing`` for an empty select filter."""
    assert resolve_labels_to_ids(["missing"], _label_map(), "Document Types", "Document Type") == ["missing"]


def test_an_unknown_label_names_the_thesaurus_and_property() -> None:
    with pytest.raises(
        SearchError, match="Value 'Nope' not found in thesaurus 'Document Types' for property 'Document Type'"
    ):
        resolve_labels_to_ids(["Nope"], _label_map(), "Document Types", "Document Type")


def test_grouped_values_resolve_by_qualified_label() -> None:
    thesauri = Thesauri.model_validate(
        {"_id": "th", "name": "Document Types", "values": [{"label": "HRC", "id": "g", "values": VALUES}]}
    )
    resolved = resolve_labels_to_ids(
        ["HRC: Resolution", "HRC: Statement"], label_id_map(thesauri.values), "Document Types", "Document Type"
    )
    assert resolved == ["id-resolution", "id-statement"]


def test_an_empty_value_list_stays_empty() -> None:
    assert resolve_labels_to_ids([], _label_map(), "Document Types", "Document Type") == []
