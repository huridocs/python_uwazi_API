from collections.abc import Callable
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, Field

from uwazi_admin_agent.domain.manifest import MigrationManifest
from uwazi_admin_agent.domain.snapshot import EntitySnapshot


class RestoreRelationshipAction(BaseModel):
    """Restore a repointed relationship to its recorded before-state."""

    action: Literal["restore_relationship"] = "restore_relationship"
    entity_internal_id: str
    relationship_type: str
    before: Any


class RestoreEntityAction(BaseModel):
    """Restore a modified entity by saving its captured raw dict back (§2.5)."""

    action: Literal["restore_entity"] = "restore_entity"
    internal_id: str
    raw: dict[str, Any]


class DeleteCreatedEntityAction(BaseModel):
    """Delete an entity created by the migration (delete by internal id)."""

    action: Literal["delete_created_entity"] = "delete_created_entity"
    internal_id: str


RevertAction: TypeAlias = Annotated[
    RestoreRelationshipAction | RestoreEntityAction | DeleteCreatedEntityAction,
    Field(discriminator="action"),
]


def build_revert_actions(
    manifest: MigrationManifest,
    load_snapshot: Callable[[str], EntitySnapshot],
) -> list[RevertAction]:
    """Build the ordered revert actions for a run (pure; §2.6).

    Ordering is: relationships first, then entity restores, then created-entity
    deletions - so references are restored before any created entity is removed,
    avoiding transient dangling pointers.

    The only "I/O" is the injected ``load_snapshot`` callable (keyed by
    ``internal_id``); this function touches no filesystem or network itself.
    """

    actions: list[RevertAction] = []

    # 1. Restore relationships first
    for rewired in manifest.rewired:
        actions.append(
            RestoreRelationshipAction(
                entity_internal_id=rewired.entity.internal_id,
                relationship_type=rewired.relationship_type,
                before=rewired.before,
            )
        )

    # 2. Restore modified entities from their snapshot
    for entity in manifest.modified:
        snapshot = load_snapshot(entity.internal_id)
        actions.append(RestoreEntityAction(internal_id=entity.internal_id, raw=snapshot.raw))

    # 3. Delete created entities last
    for created in manifest.created:
        actions.append(DeleteCreatedEntityAction(internal_id=created.internal_id))

    return actions
