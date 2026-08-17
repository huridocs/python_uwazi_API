from collections.abc import Callable
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from uwazi_admin_agent.domain.manifest import MigrationManifest
from uwazi_admin_agent.domain.snapshot import EntityIdentity, EntitySnapshot


class RestoreRelationshipAction(BaseModel):
    """Restore a rewired relationship on an entity to its recorded before-state."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["restore_relationship"] = "restore_relationship"
    entity: EntityIdentity
    property_name: str
    before: Any


class RestoreEntityAction(BaseModel):
    """Restore a modified or deleted entity from its raw snapshot.

    For a modified entity, ``save_raw(snapshot.raw)`` overwrites the current
    state. For a deleted entity, the same ``save_raw`` re-creates it (POST
    /api/entities upserts by sharedId+locale). One action covers both.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["restore_entity"] = "restore_entity"
    snapshot: EntitySnapshot


class DeleteEntityAction(BaseModel):
    """Delete an entity the script created (revert of a create)."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["delete_entity"] = "delete_entity"
    shared_id: str


RevertAction: TypeAlias = Annotated[
    RestoreRelationshipAction | RestoreEntityAction | DeleteEntityAction,
    Field(discriminator="kind"),
]


def build_revert_actions(
    manifest: MigrationManifest,
    load_snapshot: Callable[[str], EntitySnapshot],
) -> list[RevertAction]:
    """Build the ordered revert actions for a run (§2.6).

    Ordering:
    1. Restore rewired relationships — so references are in place before
       entity content is touched.
    2. Restore modified entities from their snapshots.
    3. Restore deleted entities from their snapshots (same ``save_raw`` as
       modified — POST upserts, re-creating the row).
    4. Delete created entities — **last**, so references held by restored
       entities are valid until every created entity is removed.

    Pure: touches no filesystem or network. ``load_snapshot`` is the injected
    seam that supplies a snapshot by shared id. If a snapshot for a modified
    or deleted entity cannot be loaded, the error propagates — no silent skip.
    """
    actions: list[RevertAction] = []

    for rewired in manifest.rewired:
        actions.append(
            RestoreRelationshipAction(
                entity=rewired.entity,
                property_name=rewired.property_name,
                before=rewired.before,
            )
        )

    for modified in manifest.modified:
        snapshot: EntitySnapshot = load_snapshot(modified.shared_id)
        actions.append(RestoreEntityAction(snapshot=snapshot))

    for deleted in manifest.deleted:
        snapshot = load_snapshot(deleted.shared_id)
        actions.append(RestoreEntityAction(snapshot=snapshot))

    for created in manifest.created:
        actions.append(DeleteEntityAction(shared_id=created.shared_id))

    return actions
