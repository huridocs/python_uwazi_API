from datetime import datetime, timezone
from typing import Any

import pytest

from uwazi_admin_agent.domain.manifest import MigrationManifest, RewiredRelationship, RunStatus
from uwazi_admin_agent.domain.revert import (
    DeleteEntityAction,
    RestoreEntityAction,
    RestoreRelationshipAction,
    build_revert_actions,
)
from uwazi_admin_agent.domain.snapshot import EntityIdentity, EntitySnapshot


def _identity(shared_id: str, language: str | None = None) -> EntityIdentity:
    return EntityIdentity(shared_id=shared_id, language=language)


def _snapshot(shared_id: str, raw: dict[str, Any]) -> EntitySnapshot:
    return EntitySnapshot(
        shared_id=shared_id,
        raw=raw,
        captured_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


class _SnapshotStore:
    """A tiny real in-memory snapshot loader (not a mock)."""

    def __init__(self, snapshots: dict[str, EntitySnapshot]) -> None:
        self._snapshots = snapshots
        self.requested: list[str] = []

    def __call__(self, shared_id: str) -> EntitySnapshot:
        self.requested.append(shared_id)
        return self._snapshots[shared_id]


def _manifest(
    modified: list[EntityIdentity] | None = None,
    rewired: list[RewiredRelationship] | None = None,
    created: list[EntityIdentity] | None = None,
    deleted: list[EntityIdentity] | None = None,
) -> MigrationManifest:
    return MigrationManifest(
        run_id="run-1",
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        prompt="d",
        script="x = 1",
        modified=modified or [],
        rewired=rewired or [],
        created=created or [],
        deleted=deleted or [],
        status=RunStatus.EXECUTED,
    )


# --- only modified entities --------------------------------------------------


def test_only_modified_yields_entity_restores_in_order() -> None:
    store = _SnapshotStore(
        {
            "A": _snapshot("A", {"_id": "a1", "title": "old A"}),
            "B": _snapshot("B", {"_id": "b1", "title": "old B"}),
        }
    )
    manifest = _manifest(modified=[_identity("A"), _identity("B")], rewired=[])

    actions = build_revert_actions(manifest, store)

    assert len(actions) == 2
    assert all(isinstance(a, RestoreEntityAction) for a in actions)
    assert [a.snapshot.shared_id for a in actions if isinstance(a, RestoreEntityAction)] == ["A", "B"]
    assert [a.snapshot.raw["title"] for a in actions if isinstance(a, RestoreEntityAction)] == ["old A", "old B"]
    assert store.requested == ["A", "B"]


# --- only rewired relationships ----------------------------------------------


def test_only_rewired_yields_relationship_restores() -> None:
    before_state: list[dict[str, Any]] = [{"_id": "r1", "label": "X"}]
    rewired = RewiredRelationship(
        entity=_identity("C", language="en"),
        property_name="relations",
        before=before_state,
    )
    manifest = _manifest(modified=[], rewired=[rewired])

    actions = build_revert_actions(manifest, _SnapshotStore({}))

    assert len(actions) == 1
    action = actions[0]
    assert isinstance(action, RestoreRelationshipAction)
    assert action.entity.shared_id == "C"
    assert action.property_name == "relations"
    assert action.before == before_state


# --- ordering: relationships before entity restores (§2.6) -------------------


def test_relationships_before_entity_restores() -> None:
    store = _SnapshotStore({"A": _snapshot("A", {"_id": "a1"})})
    rewired = [RewiredRelationship(entity=_identity("C"), property_name="relations", before=[])]
    modified = [_identity("A")]
    manifest = _manifest(modified=modified, rewired=rewired)

    actions = build_revert_actions(manifest, store)

    assert len(actions) == 2
    assert isinstance(actions[0], RestoreRelationshipAction)
    assert isinstance(actions[1], RestoreEntityAction)


def test_multiple_relationships_then_multiple_entities_preserve_manifest_order() -> None:
    store = _SnapshotStore({"A": _snapshot("A", {"_id": "a1"}), "B": _snapshot("B", {"_id": "b1"})})
    rewired = [
        RewiredRelationship(entity=_identity("R1"), property_name="rel1", before=[1]),
        RewiredRelationship(entity=_identity("R2"), property_name="rel2", before=[2]),
    ]
    modified = [_identity("A"), _identity("B")]
    manifest = _manifest(modified=modified, rewired=rewired)

    actions = build_revert_actions(manifest, store)

    assert [type(a) for a in actions] == [
        RestoreRelationshipAction,
        RestoreRelationshipAction,
        RestoreEntityAction,
        RestoreEntityAction,
    ]
    assert [a.entity.shared_id for a in actions[:2] if isinstance(a, RestoreRelationshipAction)] == ["R1", "R2"]
    assert [a.snapshot.shared_id for a in actions[2:] if isinstance(a, RestoreEntityAction)] == ["A", "B"]


# --- empty manifest ----------------------------------------------------------


def test_empty_manifest_yields_no_actions() -> None:
    manifest = _manifest(modified=[], rewired=[])
    assert build_revert_actions(manifest, _SnapshotStore({})) == []


# --- missing snapshot propagates (no silent skip) ---------------------------


def test_missing_snapshot_propagates() -> None:
    manifest = _manifest(modified=[_identity("missing")], rewired=[])
    with pytest.raises(KeyError):
        build_revert_actions(manifest, _SnapshotStore({}))


# --- deleted entities: restore from snapshot (same as modified) -------------


def test_deleted_restored_from_snapshot() -> None:
    store = _SnapshotStore({"X": _snapshot("X", {"_id": "x1", "title": "old X"})})
    manifest = _manifest(deleted=[_identity("X")])

    actions = build_revert_actions(manifest, store)

    assert len(actions) == 1
    assert isinstance(actions[0], RestoreEntityAction)
    assert actions[0].snapshot.shared_id == "X"
    assert actions[0].snapshot.raw["title"] == "old X"


def test_missing_snapshot_for_deleted_propagates() -> None:
    manifest = _manifest(deleted=[_identity("gone")])
    with pytest.raises(KeyError):
        build_revert_actions(manifest, _SnapshotStore({}))


# --- created entities: delete action, last in order (§2.6) ------------------


def test_created_yields_delete_action() -> None:
    manifest = _manifest(created=[_identity("NEW1"), _identity("NEW2")])

    actions = build_revert_actions(manifest, _SnapshotStore({}))

    assert len(actions) == 2
    assert all(isinstance(a, DeleteEntityAction) for a in actions)
    assert [a.shared_id for a in actions] == ["NEW1", "NEW2"]


# --- full ordering: relationships → modified → deleted → created (§2.6) ----


def test_full_order_relationships_modified_deleted_created() -> None:
    store = _SnapshotStore(
        {
            "M": _snapshot("M", {"_id": "m1"}),
            "D": _snapshot("D", {"_id": "d1"}),
        }
    )
    rewired = [RewiredRelationship(entity=_identity("R"), property_name="relations", before=[])]
    manifest = _manifest(
        modified=[_identity("M")],
        rewired=rewired,
        created=[_identity("C")],
        deleted=[_identity("D")],
    )

    actions = build_revert_actions(manifest, store)

    assert [type(a) for a in actions] == [
        RestoreRelationshipAction,
        RestoreEntityAction,
        RestoreEntityAction,
        DeleteEntityAction,
    ]
    assert actions[0].entity.shared_id == "R"
    assert actions[1].snapshot.shared_id == "M"
    assert actions[2].snapshot.shared_id == "D"
    assert actions[3].shared_id == "C"


def test_created_deletions_after_deleted_restores() -> None:
    store = _SnapshotStore({"D": _snapshot("D", {"_id": "d1"})})
    manifest = _manifest(deleted=[_identity("D")], created=[_identity("C")])

    actions = build_revert_actions(manifest, store)

    assert isinstance(actions[0], RestoreEntityAction)
    assert actions[0].snapshot.shared_id == "D"
    assert isinstance(actions[1], DeleteEntityAction)
    assert actions[1].shared_id == "C"


def test_only_created_yields_only_deletions() -> None:
    manifest = _manifest(created=[_identity("A"), _identity("B")])
    actions = build_revert_actions(manifest, _SnapshotStore({}))
    assert len(actions) == 2
    assert all(isinstance(a, DeleteEntityAction) for a in actions)


def test_modified_and_deleted_same_ordering_preserves_manifest_order() -> None:
    store = _SnapshotStore(
        {
            "M1": _snapshot("M1", {}),
            "M2": _snapshot("M2", {}),
            "D1": _snapshot("D1", {}),
            "D2": _snapshot("D2", {}),
        }
    )
    manifest = _manifest(
        modified=[_identity("M1"), _identity("M2")],
        deleted=[_identity("D1"), _identity("D2")],
        created=[_identity("C1")],
    )

    actions = build_revert_actions(manifest, store)

    assert [type(a) for a in actions] == [
        RestoreEntityAction,
        RestoreEntityAction,
        RestoreEntityAction,
        RestoreEntityAction,
        DeleteEntityAction,
    ]
    assert [a.snapshot.shared_id for a in actions[:4]] == ["M1", "M2", "D1", "D2"]
    assert actions[4].shared_id == "C1"
