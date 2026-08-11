from collections.abc import Callable
from datetime import datetime

from uwazi_admin_agent.domain.filter import EntityFilter
from uwazi_admin_agent.domain.manifest import (
    EntityIdentity,
    MigrationManifest,
    RewiredRelationship,
    RunStatus,
)
from uwazi_admin_agent.domain.ops import SetPropertyOp
from uwazi_admin_agent.domain.plan import MigrationPlan
from uwazi_admin_agent.domain.revert import (
    DeleteCreatedEntityAction,
    RestoreEntityAction,
    RestoreRelationshipAction,
    build_revert_actions,
)
from uwazi_admin_agent.domain.snapshot import EntitySnapshot


def _plan() -> MigrationPlan:
    return MigrationPlan(
        description="d",
        ops=[SetPropertyOp(filter=EntityFilter(template="Court"), property_name="x", value=1)],
    )


def _manifest(
    *,
    modified: list[EntityIdentity] | None = None,
    created: list[EntityIdentity] | None = None,
    rewired: list[RewiredRelationship] | None = None,
) -> MigrationManifest:
    return MigrationManifest(
        run_id="r1",
        created_at=datetime(2026, 1, 1),
        plan=_plan(),
        modified=modified or [],
        created=created or [],
        rewired=rewired or [],
        status=RunStatus.executed,
        snapshots_location="/tmp/runs/r1",
    )


def _snap(internal_id: str) -> EntitySnapshot:
    return EntitySnapshot(
        internal_id=internal_id,
        shared_id=f"s_{internal_id}",
        language="en",
        captured_at=datetime(2026, 1, 1),
        raw={"_id": internal_id, "sharedId": f"s_{internal_id}", "title": "t"},
    )


def _store_loader(snapshots: dict[str, EntitySnapshot]) -> Callable[[str], EntitySnapshot]:
    def load(internal_id: str) -> EntitySnapshot:
        return snapshots[internal_id]

    return load


def test_only_modified_yields_restore_entity_actions() -> None:
    manifest = _manifest(modified=[EntityIdentity(internal_id="i1", shared_id="s1", language="en")])
    actions = build_revert_actions(manifest, _store_loader({"i1": _snap("i1")}))
    assert len(actions) == 1
    assert isinstance(actions[0], RestoreEntityAction)
    assert actions[0].internal_id == "i1"
    assert actions[0].raw == {"_id": "i1", "sharedId": "s_i1", "title": "t"}


def test_created_yields_delete_actions() -> None:
    manifest = _manifest(created=[EntityIdentity(internal_id="i9", shared_id="s9")])
    actions = build_revert_actions(manifest, _store_loader({}))
    assert len(actions) == 1
    assert isinstance(actions[0], DeleteCreatedEntityAction)
    assert actions[0].internal_id == "i9"


def test_rewired_yields_relationship_restore_action() -> None:
    manifest = _manifest(
        rewired=[
            RewiredRelationship(
                entity=EntityIdentity(internal_id="i2", shared_id="s2"),
                relationship_type="relates_to",
                before=[{"entity": "i_old"}],
            )
        ]
    )
    actions = build_revert_actions(manifest, _store_loader({}))
    assert len(actions) == 1
    assert isinstance(actions[0], RestoreRelationshipAction)
    assert actions[0].entity_internal_id == "i2"
    assert actions[0].relationship_type == "relates_to"
    assert actions[0].before == [{"entity": "i_old"}]


def test_ordering_relationships_then_restores_then_deletions() -> None:
    manifest = _manifest(
        modified=[EntityIdentity(internal_id="i1", shared_id="s1", language="en")],
        created=[EntityIdentity(internal_id="i9", shared_id="s9")],
        rewired=[
            RewiredRelationship(
                entity=EntityIdentity(internal_id="i2", shared_id="s2"),
                relationship_type="relates_to",
                before=[{"entity": "i_old"}],
            )
        ],
    )
    actions = build_revert_actions(manifest, _store_loader({"i1": _snap("i1")}))
    assert [type(a).__name__ for a in actions] == [
        "RestoreRelationshipAction",
        "RestoreEntityAction",
        "DeleteCreatedEntityAction",
    ]


def test_loader_called_only_for_modified_entities() -> None:
    loaded: list[str] = []

    def load(internal_id: str) -> EntitySnapshot:
        loaded.append(internal_id)
        return _snap(internal_id)

    manifest = _manifest(
        modified=[EntityIdentity(internal_id="i1", shared_id="s1")],
        created=[EntityIdentity(internal_id="i9", shared_id="s9")],
        rewired=[
            RewiredRelationship(
                entity=EntityIdentity(internal_id="i2", shared_id="s2"),
                relationship_type="relates_to",
                before=[],
            )
        ],
    )
    build_revert_actions(manifest, load)
    # Only the modified entity needs a snapshot; relationships carry their
    # before-state in the manifest, and created entities are deleted, not restored.
    assert loaded == ["i1"]


def test_empty_manifest_yields_no_actions() -> None:
    actions = build_revert_actions(_manifest(), _store_loader({}))
    assert actions == []
