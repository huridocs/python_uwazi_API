"""Isolated unit tests for :mod:`uwazi_admin_agent.domain.relationship_restore`.

Pure transforms: literal snapshot dicts + plain assertions. No I/O, no mocks.
"""

from datetime import datetime, timezone
from typing import Any

import pytest

from uwazi_admin_agent.domain.relationship_restore import (
    CapturedHub,
    InboundRef,
    extract_inbound_refs_from_existing,
    extract_mutual_deleted_hubs,
    has_co_deleted_refs,
    remap_metadata_refs,
)
from uwazi_admin_agent.domain.snapshot import EntitySnapshot


def _snapshot(shared_id: str, raw: dict[str, Any]) -> EntitySnapshot:
    return EntitySnapshot(
        shared_id=shared_id,
        internal_id=raw.get("_id"),
        language=raw.get("language"),
        raw=raw,
        captured_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def _rel(entity: str, hub: str, template: str | None) -> dict[str, Any]:
    return {"entity": entity, "hub": hub, "template": template}


# --- extract_mutual_deleted_hubs -------------------------------------------


def test_extract_keeps_hub_whose_endpoints_are_all_deleted() -> None:
    snap_a = _snapshot("A", {"sharedId": "A", "relations": [_rel("A", "h1", None), _rel("B", "h1", "rtype1")]})
    snap_b = _snapshot("B", {"sharedId": "B", "relations": [_rel("A", "h1", None), _rel("B", "h1", "rtype1")]})

    hubs = extract_mutual_deleted_hubs({"A": snap_a, "B": snap_b}, {"A", "B"})

    assert len(hubs) == 1
    assert hubs[0] == CapturedHub(hub="h1", from_shared_id="A", to_shared_id="B", relation_type="rtype1")


def test_extract_drops_hub_to_a_still_existing_entity() -> None:
    snap_a = _snapshot(
        "A",
        {
            "sharedId": "A",
            "relations": [
                _rel("A", "h1", None),
                _rel("B", "h1", "rtype1"),
                _rel("A", "h2", None),
                _rel("STATE", "h2", "rtype1"),
            ],
        },
    )
    snap_b = _snapshot("B", {"sharedId": "B", "relations": [_rel("A", "h1", None), _rel("B", "h1", "rtype1")]})

    hubs = extract_mutual_deleted_hubs({"A": snap_a, "B": snap_b}, {"A", "B"})

    assert [h.hub for h in hubs] == ["h1"]


def test_extract_dedups_hub_seen_in_both_snapshots() -> None:
    snap_a = _snapshot("A", {"sharedId": "A", "relations": [_rel("A", "h1", None), _rel("B", "h1", "rtype1")]})
    snap_b = _snapshot("B", {"sharedId": "B", "relations": [_rel("A", "h1", None), _rel("B", "h1", "rtype1")]})

    hubs = extract_mutual_deleted_hubs({"A": snap_a, "B": snap_b}, {"A", "B"})

    assert len(hubs) == 1


def test_extract_returns_empty_when_no_relations() -> None:
    snap_a = _snapshot("A", {"sharedId": "A"})
    assert extract_mutual_deleted_hubs({"A": snap_a}, {"A"}) == []


def test_extract_returns_empty_when_no_mutual_hubs() -> None:
    snap_a = _snapshot("A", {"sharedId": "A", "relations": [_rel("A", "h1", None), _rel("STATE", "h1", "rtype1")]})
    assert extract_mutual_deleted_hubs({"A": snap_a}, {"A"}) == []


def test_extract_preserves_from_to_assignment_by_template() -> None:
    snap_a = _snapshot(
        "A",
        {"sharedId": "A", "relations": [_rel("B", "h1", "rtype7"), _rel("A", "h1", None)]},
    )
    hubs = extract_mutual_deleted_hubs({"A": snap_a, "B": _snapshot("B", {"sharedId": "B"})}, {"A", "B"})

    assert hubs[0].from_shared_id == "A"
    assert hubs[0].to_shared_id == "B"
    assert hubs[0].relation_type == "rtype7"


def test_extract_drops_hub_with_no_typed_to_row() -> None:
    snap_a = _snapshot("A", {"sharedId": "A", "relations": [_rel("A", "h1", None), _rel("B", "h1", None)]})
    assert extract_mutual_deleted_hubs({"A": snap_a}, {"A", "B"}) == []


def test_extract_handles_string_or_non_string_deleted_ids() -> None:
    snap_a = _snapshot("A", {"sharedId": "A", "relations": [_rel("A", "h1", None), _rel("B", "h1", "rtype1")]})
    hubs = extract_mutual_deleted_hubs({"A": snap_a}, {"A", "B"})
    assert len(hubs) == 1


# --- extract_inbound_refs_from_existing ------------------------------------


def test_inbound_keeps_ref_from_existing_to_deleted() -> None:
    # A was deleted; B (still exists) had a ref to A: hub from=B(null), to=A(rtype1).
    snap_a = _snapshot(
        "A",
        {"sharedId": "A", "template": "tmplA", "relations": [_rel("B", "h1", None), _rel("A", "h1", "rtype1")]},
    )

    refs = extract_inbound_refs_from_existing({"A": snap_a}, {"A"})

    assert refs == [
        InboundRef(
            existing_shared_id="B",
            deleted_shared_id="A",
            relation_type="rtype1",
            deleted_template_id="tmplA",
        )
    ]


def test_inbound_drops_ref_from_deleted_to_existing() -> None:
    # A (deleted) had a ref to STATE (still exists): from=A, to=STATE — not inbound.
    snap_a = _snapshot("A", {"sharedId": "A", "relations": [_rel("A", "h1", None), _rel("STATE", "h1", "rtype1")]})
    assert extract_inbound_refs_from_existing({"A": snap_a}, {"A"}) == []


def test_inbound_drops_mutual_both_deleted_ref() -> None:
    # A and B both deleted; the B->A hub has from=B (deleted) — not an existing source.
    snap_a = _snapshot("A", {"sharedId": "A", "relations": [_rel("B", "h1", None), _rel("A", "h1", "rtype1")]})
    assert extract_inbound_refs_from_existing({"A": snap_a}, {"A", "B"}) == []


def test_inbound_excludes_existing_entities_in_the_manifest() -> None:
    snap_a = _snapshot(
        "A", {"sharedId": "A", "template": "t", "relations": [_rel("B", "h1", None), _rel("A", "h1", "rtype1")]}
    )
    # B is itself in the manifest (modified) -> stale-id limitation, excluded.
    refs = extract_inbound_refs_from_existing({"A": snap_a}, {"A"}, excluded_existing={"B"})
    assert refs == []


def test_inbound_dedups_ref_seen_in_both_snapshots() -> None:
    # The B->A hub appears in both A's and B's snapshots; emitted once.
    snap_a = _snapshot(
        "A", {"sharedId": "A", "template": "t", "relations": [_rel("B", "h1", None), _rel("A", "h1", "rtype1")]}
    )
    snap_b = _snapshot("B", {"sharedId": "B", "relations": [_rel("B", "h1", None), _rel("A", "h1", "rtype1")]})
    refs = extract_inbound_refs_from_existing({"A": snap_a, "B": snap_b}, {"A"})
    assert len(refs) == 1
    assert refs[0].existing_shared_id == "B"


def test_inbound_drops_hub_with_no_typed_to_row() -> None:
    snap_a = _snapshot("A", {"sharedId": "A", "relations": [_rel("B", "h1", None), _rel("A", "h1", None)]})
    assert extract_inbound_refs_from_existing({"A": snap_a}, {"A"}) == []


def test_inbound_carries_deleted_template_id_for_content_filter() -> None:
    snap_a = _snapshot(
        "A", {"sharedId": "A", "template": "tmplA", "relations": [_rel("B", "h1", None), _rel("A", "h1", "rtype1")]}
    )
    refs = extract_inbound_refs_from_existing({"A": snap_a}, {"A"})
    assert refs[0].deleted_template_id == "tmplA"


# --- has_co_deleted_refs ---------------------------------------------------


def test_has_co_deleted_refs_true_when_a_ref_targets_a_co_deleted_id() -> None:
    metadata = {"entity_relation": [{"value": "B", "label": "B"}, {"value": "STATE", "label": "S"}]}
    assert has_co_deleted_refs(metadata, {"B", "A"}) is True


def test_has_co_deleted_refs_false_when_refs_only_target_still_existing() -> None:
    metadata = {"entity_relation": [{"value": "STATE", "label": "S"}]}
    assert has_co_deleted_refs(metadata, {"A"}) is False


def test_has_co_deleted_refs_detects_self_ref() -> None:
    metadata = {"entity_relation": [{"value": "A", "label": "A"}]}
    assert has_co_deleted_refs(metadata, {"A"}) is True


def test_has_co_deleted_refs_ignores_non_relationship_values() -> None:
    metadata = {"status": [{"value": "uuid-1"}], "date": [{"value": 1700000000}], "title": "plain"}
    assert has_co_deleted_refs(metadata, {"A"}) is False


# --- remap_metadata_refs ---------------------------------------------------


def test_remap_replaces_old_shared_id_with_new() -> None:
    metadata = {"entity_relation": [{"value": "B", "label": "B title"}, {"value": "STATE", "label": "State"}]}
    remapped = remap_metadata_refs(metadata, {"B": "newB"})
    assert remapped["entity_relation"] == [
        {"value": "newB", "label": "B title"},
        {"value": "STATE", "label": "State"},
    ]


def test_remap_leaves_non_relationship_values_untouched() -> None:
    metadata = {
        "status": [{"value": "uuid-1", "label": "Final"}],
        "date": [{"value": 1700000000}],
        "title": "plain",
        "tags": ["x"],
    }
    assert remap_metadata_refs(metadata, {"B": "newB"}) == metadata


def test_remap_does_not_mutate_input() -> None:
    metadata = {"entity_relation": [{"value": "B", "label": "B"}]}
    remap_metadata_refs(metadata, {"B": "newB"})
    assert metadata["entity_relation"][0]["value"] == "B"


def test_remap_empty_id_map_returns_equivalent() -> None:
    metadata = {"entity_relation": [{"value": "B", "label": "B"}]}
    assert remap_metadata_refs(metadata, {}) == metadata


# --- removed bulk payload guard -------------------------------------------


def test_build_relationship_recreate_payload_removed_raises() -> None:
    from uwazi_admin_agent.domain.relationship_restore import build_relationship_recreate_payload

    with pytest.raises(NotImplementedError):
        build_relationship_recreate_payload([], {})


# --- CapturedHub / InboundRef are frozen ----------------------------------


def test_captured_hub_is_frozen() -> None:
    hub = CapturedHub(hub="h1", from_shared_id="A", to_shared_id="B", relation_type="r")
    with pytest.raises(Exception):
        hub.from_shared_id = "Z"  # type: ignore[misc]


def test_inbound_ref_is_frozen() -> None:
    ref = InboundRef(existing_shared_id="B", deleted_shared_id="A", relation_type="r")
    with pytest.raises(Exception):
        ref.existing_shared_id = "Z"  # type: ignore[misc]
