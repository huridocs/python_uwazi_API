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
    """Restore a modified entity from its raw snapshot.

    ``save_raw(snapshot.raw)`` overwrites the current state via the POST
    /api/entities update branch (the raw carries its original ``_id``/``sharedId``,
    which the update branch requires). Used for entities the script *modified*.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["restore_entity"] = "restore_entity"
    snapshot: EntitySnapshot


class RecreateEntityAction(BaseModel):
    """Re-create a deleted entity from its raw snapshot (exact-data revert).

    The script deleted the entity, so its row is gone and the POST /api/entities
    update branch (used by :class:`RestoreEntityAction`) would 422. Revert instead
    goes through the **create branch** (no ``sharedId`` in the body), which mints a
    fresh ``sharedId``/``_id`` and restores the entity's DATA fields (title,
    template, icon, user, metadata, url-attachments). Identity is intentionally not
    preserved — this is exact-data, not exact-identity, revert for the delete case
    (see ``domain/create_payload.py`` for what is and isn't restorable).
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["recreate_entity"] = "recreate_entity"
    snapshot: EntitySnapshot


class DeleteEntityAction(BaseModel):
    """Delete an entity the script created (revert of a create)."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["delete_entity"] = "delete_entity"
    shared_id: str


RevertAction: TypeAlias = Annotated[
    RestoreRelationshipAction | RestoreEntityAction | RecreateEntityAction | DeleteEntityAction,
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
    2. Restore modified entities from their snapshots (update branch).
    3. Re-create deleted entities from their snapshots (create branch — a new
       sharedId is minted; identity is not preserved, only data).
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
        actions.append(RecreateEntityAction(snapshot=snapshot))

    for created in manifest.created:
        actions.append(DeleteEntityAction(shared_id=created.shared_id))

    return actions
