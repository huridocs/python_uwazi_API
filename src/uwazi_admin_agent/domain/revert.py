from collections.abc import Callable
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from uwazi_admin_agent.domain.manifest import MigrationManifest
from uwazi_admin_agent.domain.relationship_restore import (
    InboundRef,
    extract_inbound_refs_from_existing,
    has_co_deleted_refs,
)
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


class ReapplyRelationshipRefsAction(BaseModel):
    """Re-apply relationship refs after deleted entities are re-created (delete-revert).

    Replaces the former bulk ``POST /api/relationships/bulk`` approach, which
    forced ``updateEntities=true`` and re-derived metadata WITHOUT excluding
    ``r.entity === entity.sharedId`` (``buildRelationshipMetadata``), producing
    self-refs (A'→A'). The entity-save path used here — ``save_raw`` →
    ``saveEntityBasedReferences`` with ``updateEntities=false`` — builds hubs
    FROM the posted metadata and never re-derives it, so no self-refs appear.

    Two scopes, both pure-derived from the snapshots at build time and remapped
    to the NEW sharedIds at execution (the new ids are only known after the
    ``RecreateEntityAction``s run):
    - ``recreate_targets``: OLD sharedIds of re-created deleted entities whose
      snapshot metadata had a ref to a **co-deleted** entity. For each, the use
      case fetches the re-created entity's current raw (by its NEW sharedId),
      sets ``metadata = remap_metadata_refs(snapshot.metadata, id_map)``, and
      ``save_raw``-s it; ``saveEntityBasedReferences`` rebuilds the outgoing
      hub(s) from the remapped metadata. A deleted entity whose only refs are to
      still-existing entities is NOT listed (those hubs are auto-restored on
      create, so a re-save would be a needless no-op).
    - ``inbound_targets``: refs from **still-existing** entities to deleted ones
      (``InboundRef``), whose metadata ref was cascade-removed on delete. For
      each (grouped by existing entity), the use case fetches the existing
      entity's current raw, resolves the property name on its template, appends
      the remapped ``{value: NEW, label}`` entry, and ``save_raw``-s it.
      Existing entities that are themselves in the manifest are excluded — that
      stale-id edge case is a documented limitation.

    Emitted **after** every ``RecreateEntityAction`` (both endpoints must exist
    with their NEW sharedIds before any hub is rebuilt) and **before**
    ``DeleteEntityAction``s. Empty when no deleted entity shared/co-held a
    relationship ref with another entity.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["reapply_relationship_refs"] = "reapply_relationship_refs"
    recreate_targets: list[str] = Field(default_factory=list)
    inbound_targets: list[InboundRef] = Field(default_factory=list)


RevertAction: TypeAlias = Annotated[
    RestoreRelationshipAction
    | RestoreEntityAction
    | RecreateEntityAction
    | ReapplyRelationshipRefsAction
    | DeleteEntityAction,
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
    4. Re-apply relationship refs — AFTER both endpoints are re-created, so the
       NEW sharedIds exist when the entity-save path's ``saveEntityBasedReferences``
       rebuilds the hubs from remapped metadata. Two scopes: re-save each
       re-created entity whose metadata referenced a co-deleted entity (remapped
       old→new), and re-add the cascade-stripped ref on still-existing entities
       that referenced a deleted entity. Hubs to still-existing entities that
       were NOT cascade-stripped (the deleted entity's own outgoing refs) are
       auto-restored by the create path and not re-applied here.
    5. Delete created entities — **last**, so references held by restored
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

    deleted_snapshots: dict[str, EntitySnapshot] = {}
    for deleted in manifest.deleted:
        snap = load_snapshot(deleted.shared_id)
        deleted_snapshots[deleted.shared_id] = snap
        actions.append(RecreateEntityAction(snapshot=snap))

    deleted_ids = {deleted.shared_id for deleted in manifest.deleted}
    recreate_targets = [
        sid
        for sid, snap in deleted_snapshots.items()
        if has_co_deleted_refs((snap.raw.get("metadata") or {}) if isinstance(snap.raw, dict) else {}, deleted_ids)
    ]
    manifest_ids = (
        {e.shared_id for e in manifest.modified}
        | {e.shared_id for e in manifest.deleted}
        | {e.shared_id for e in manifest.created}
    )
    inbound_targets = extract_inbound_refs_from_existing(deleted_snapshots, deleted_ids, excluded_existing=manifest_ids)
    if recreate_targets or inbound_targets:
        actions.append(
            ReapplyRelationshipRefsAction(
                recreate_targets=recreate_targets,
                inbound_targets=inbound_targets,
            )
        )

    for created in manifest.created:
        actions.append(DeleteEntityAction(shared_id=created.shared_id))

    return actions
