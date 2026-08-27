from datetime import datetime, timezone
from typing import Any

import pytest

from uwazi_admin_agent.domain.manifest import MigrationManifest, RewiredRelationship, RunStatus
from uwazi_admin_agent.domain.revert import (
    DeleteEntityAction,
    ReapplyRelationshipRefsAction,
    RecreateEntityAction,
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


# --- deleted entities: re-create from snapshot (new sharedId, exact-data revert) ---


def test_deleted_yields_recreate_action() -> None:
    store = _SnapshotStore({"X": _snapshot("X", {"_id": "x1", "title": "old X"})})
    manifest = _manifest(deleted=[_identity("X")])

    actions = build_revert_actions(manifest, store)

    assert len(actions) == 1
    assert isinstance(actions[0], RecreateEntityAction)
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
        RecreateEntityAction,
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

    assert isinstance(actions[0], RecreateEntityAction)
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
        RecreateEntityAction,
        RecreateEntityAction,
        DeleteEntityAction,
    ]
    assert [a.snapshot.shared_id for a in actions[:4]] == ["M1", "M2", "D1", "D2"]
    assert actions[4].shared_id == "C1"


# --- relationship ref re-apply AFTER entity re-creates (§2.6) ---------------


def _rel(entity: str, hub: str, template: str | None) -> dict:
    return {"entity": entity, "hub": hub, "template": template}


def test_mutual_deleted_yields_reapply_after_entity_recreates() -> None:
    # A and B relate to each other (hub h1); both deleted and both hold a co-deleted
    # metadata ref, so each is a re-create target for the re-apply action.
    store = _SnapshotStore(
        {
            "A": _snapshot(
                "A",
                {
                    "metadata": {"entity_relation": [{"value": "B", "label": "B"}]},
                    "relations": [_rel("A", "h1", None), _rel("B", "h1", "rtype1")],
                },
            ),
            "B": _snapshot(
                "B",
                {
                    "metadata": {"entity_relation": [{"value": "A", "label": "A"}]},
                    "relations": [_rel("A", "h1", None), _rel("B", "h1", "rtype1")],
                },
            ),
        }
    )
    manifest = _manifest(deleted=[_identity("A"), _identity("B")], created=[_identity("C")])

    actions = build_revert_actions(manifest, store)

    assert [type(a) for a in actions] == [
        RecreateEntityAction,
        RecreateEntityAction,
        ReapplyRelationshipRefsAction,
        DeleteEntityAction,
    ]
    rel_action = actions[2]
    assert isinstance(rel_action, ReapplyRelationshipRefsAction)
    assert rel_action.recreate_targets == ["A", "B"]
    assert rel_action.inbound_targets == []


def test_reapply_action_after_recreates_before_delete_created() -> None:
    store = _SnapshotStore(
        {
            "A": _snapshot(
                "A",
                {
                    "metadata": {"entity_relation": [{"value": "B", "label": "B"}]},
                    "relations": [_rel("A", "h1", None), _rel("B", "h1", "rtype1")],
                },
            ),
            "B": _snapshot(
                "B",
                {
                    "metadata": {"entity_relation": [{"value": "A", "label": "A"}]},
                    "relations": [_rel("A", "h1", None), _rel("B", "h1", "rtype1")],
                },
            ),
        }
    )
    manifest = _manifest(deleted=[_identity("A"), _identity("B")], created=[_identity("NEW")])

    actions = build_revert_actions(manifest, store)

    types = [type(a) for a in actions]
    assert types.index(ReapplyRelationshipRefsAction) > types.index(RecreateEntityAction)
    assert types.index(ReapplyRelationshipRefsAction) < types.index(DeleteEntityAction)


def test_no_reapply_action_when_deleted_have_only_still_existing_refs() -> None:
    # D relates only to a still-existing entity (STATE) and holds no co-deleted ref;
    # STATE is not in the manifest, but the hub is FROM=D (deleted) so it is not an
    # inbound ref either — no re-apply action is emitted.
    store = _SnapshotStore(
        {
            "D": _snapshot(
                "D",
                {
                    "metadata": {"entity_relation": [{"value": "STATE", "label": "State"}]},
                    "relations": [_rel("D", "h1", None), _rel("STATE", "h1", "rtype1")],
                },
            )
        }
    )
    manifest = _manifest(deleted=[_identity("D")])

    actions = build_revert_actions(manifest, store)

    assert [type(a) for a in actions] == [RecreateEntityAction]


def test_reapply_action_carries_inbound_ref_from_still_existing_entity() -> None:
    # A is deleted; B (still exists, NOT in the manifest) had a ref to A — the hub
    # from=B(null), to=A(rtype1). A's own metadata has no co-deleted ref, so
    # recreate_targets is empty, but the inbound ref on B is captured for restore.
    store = _SnapshotStore(
        {
            "A": _snapshot(
                "A",
                {
                    "template": "tmplA",
                    "metadata": {"entity_relation": [{"value": "STATE", "label": "State"}]},
                    "relations": [
                        _rel("A", "h2", None),
                        _rel("STATE", "h2", "rtype1"),
                        _rel("B", "h1", None),
                        _rel("A", "h1", "rtype1"),
                    ],
                },
            )
        }
    )
    manifest = _manifest(deleted=[_identity("A")])

    actions = build_revert_actions(manifest, store)

    assert [type(a) for a in actions] == [RecreateEntityAction, ReapplyRelationshipRefsAction]
    rel_action = actions[1]
    assert isinstance(rel_action, ReapplyRelationshipRefsAction)
    assert rel_action.recreate_targets == []
    assert len(rel_action.inbound_targets) == 1
    ref = rel_action.inbound_targets[0]
    assert ref.existing_shared_id == "B"
    assert ref.deleted_shared_id == "A"
    assert ref.relation_type == "rtype1"
    assert ref.deleted_template_id == "tmplA"


def test_reapply_action_excludes_inbound_refs_on_manifest_members() -> None:
    # B is in the manifest (modified) AND had a ref to deleted A — the stale-id
    # limitation excludes B from inbound restore; no re-apply action (A has no
    # co-deleted ref either).
    store = _SnapshotStore(
        {
            "A": _snapshot(
                "A",
                {
                    "metadata": {"entity_relation": [{"value": "STATE", "label": "State"}]},
                    "relations": [_rel("B", "h1", None), _rel("A", "h1", "rtype1")],
                },
            ),
            "B": _snapshot("B", {"_id": "b1"}),
        }
    )
    manifest = _manifest(modified=[_identity("B")], deleted=[_identity("A")])

    actions = build_revert_actions(manifest, store)

    assert [type(a) for a in actions] == [RestoreEntityAction, RecreateEntityAction]


def test_reapply_relationship_refs_action_is_frozen() -> None:
    action = ReapplyRelationshipRefsAction()
    with pytest.raises(Exception):
        action.recreate_targets = []  # type: ignore[misc]
