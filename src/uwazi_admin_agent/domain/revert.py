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
    """Restore a modified entity from its raw snapshot."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["restore_entity"] = "restore_entity"
    snapshot: EntitySnapshot


# Forward-compatible union: Phase 9 may add a DeleteEntityAction for create-ops.
RevertAction: TypeAlias = Annotated[RestoreRelationshipAction | RestoreEntityAction, Field(discriminator="kind")]


def build_revert_actions(
    manifest: MigrationManifest,
    load_snapshot: Callable[[str], EntitySnapshot],
) -> list[RevertAction]:
    """Build the ordered revert actions for a run (§2.6).

    Ordering: restore relationships first, then restore modified entities — so
    references are restored before any entity content is touched. No
    delete-created step (no current op creates entities; Phase 9 if that lands).

    Pure: touches no filesystem or network. ``load_snapshot`` is the injected
    seam that supplies a snapshot by shared id. If a snapshot for a modified
    entity cannot be loaded, the error propagates - no silent skip.
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

    return actions
