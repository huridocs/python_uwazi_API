from collections.abc import Callable
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from uwazi_admin_agent.domain.connection_hubs import rows_to_delete
from uwazi_admin_agent.domain.deleted_file import DeletedFile
from uwazi_admin_agent.domain.manifest import MigrationManifest
from uwazi_admin_agent.domain.publish_state import PublishState
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


class RestoreDeletedFilesAction(BaseModel):
    """Re-upload the run's deleted FILE rows (explicit deletes + dedupe cleanups).

    Uwazi mints a fresh ``_id`` + storage filename on every upload, so the
    restore is by CONTENT, not identity: each record's captured bytes are
    loaded from the backup store (keyed ``(run_id, shared_id, file_id)``) and
    re-uploaded to the target ``shared_id`` — the entity's CURRENT sharedId,
    or its re-created NEW sharedId when the same run also deleted the entity
    (which is why this action runs AFTER every ``RecreateEntityAction``).
    Documents are ordered before attachments (mirroring
    :func:`build_file_restore_actions` so the primary PDF lands first).
    Re-uploading a ``dedupe``-source delete RE-CREATES a duplicate copy — the
    correct undo of a dedupe cleanup, and the revert summary says so.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["restore_deleted_files"] = "restore_deleted_files"
    files: list[DeletedFile] = Field(default_factory=list)


class RestorePublishStateAction(BaseModel):
    """Restore a modified entity's publish + permission grants to their before-state.

    ``POST /api/entities`` is the content restore path, and Uwazi strips
    ``published``/``permissions`` from it (the access policy lives in its own
    collection). Without this action a revert of a publish/unpublish or a
    permission change reported success while the entity stayed public (or
    private). The captured grants are posted verbatim to
    ``POST /api/entities/permissions``, whose handler derives ``isPublic`` from
    the public grant's presence — so publishing, unpublishing and per-user
    grants are all restored by the same call.

    Emitted for every ``manifest.modified`` entity whose snapshot carries a
    ``publish_state`` (legacy snapshots carry none and are skipped).
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["restore_publish_state"] = "restore_publish_state"
    shared_id: str
    state: PublishState


class RestoreConnectionHubsAction(BaseModel):
    """Re-create the connection rows a metadata restore cannot reproduce.

    Rows carrying ``file`` (file-cited connections) or ``reference`` (text
    citations) belong to no relationship property, so ``POST /api/entities``
    never rebuilds them. They are captured at first touch on the snapshot
    (:attr:`EntitySnapshot.connection_hubs`) and replayed through
    ``POST /api/relationships/bulk``.

    For a MODIFIED entity the restore is exact: rows the run added are removed
    and the captured set is re-saved. For a RE-CREATED (deleted) entity the
    rows are remapped to the NEW sharedId and re-saved (nothing to remove — the
    old rows died with the old entity).

    Emitted after every ``RecreateEntityAction`` (a re-created entity's endpoints
    must exist) and before ``DeleteEntityAction``s.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["restore_connection_hubs"] = "restore_connection_hubs"
    shared_id: str = Field(description="The entity whose hubs are restored (OLD sharedId for a re-create).")
    target_shared_id: str | None = Field(
        default=None,
        description="For a re-created entity: the NEW sharedId the captured rows are remapped onto.",
    )
    language: str | None = Field(default=None, description="Row language the rows were captured on.")
    rows: list[dict[str, Any]] = Field(default_factory=list)
    current_row_ids: list[str] = Field(
        default_factory=list,
        description=(
            "For a MODIFIED entity: the connection-row _ids live at revert-build time that the "
            "captured state does not contain — deleted to undo a hub the run created. Empty for "
            "re-created entities (their old rows are already gone)."
        ),
    )


RevertAction: TypeAlias = Annotated[
    RestoreRelationshipAction
    | RestoreEntityAction
    | RecreateEntityAction
    | ReapplyRelationshipRefsAction
    | RestoreDeletedFilesAction
    | RestorePublishStateAction
    | RestoreConnectionHubsAction
    | DeleteEntityAction,
    Field(discriminator="kind"),
]


def build_revert_actions(
    manifest: MigrationManifest,
    load_snapshot: Callable[[str], EntitySnapshot],
    load_current_hubs: Callable[[str, str | None], list[dict[str, Any]]] | None = None,
    shared_ids: set[str] | None = None,
) -> list[RevertAction]:
    """Build the ordered revert actions for a run (§2.6).

    ``shared_ids`` narrows the list to the entities of ONE operation (the
    per-row Revert): a merge row owns its target AND the sources it deleted, so
    its undo is exactly this run's actions for those entities, in this same
    order. ``None`` (the default) is the whole run. Two consequences of a
    narrowed list:

    - The run's standalone deleted FILE rows are left out: those belong to the
      file-delete operation that recorded them, not to the entity being
      restored (an entity's own files ride its snapshot).
    - Entities OUTSIDE the scope are not treated as "handled elsewhere" for the
      inbound-ref restore, so an in-scope entity that referenced a re-created
      source (the merge target, typically) gets its stripped ref re-added.

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
    5. Re-upload the run's deleted FILE rows (:class:`RestoreDeletedFilesAction`)
       — after the re-creates + relationship re-apply, because a file whose
       entity was also deleted must land on the NEW sharedId (minted by step 3),
       and file rows are orthogonal to entity raws (a file re-upload never
       interferes with a relationship re-save). Still-existing entities keep
       their sharedId. Best-effort per file, like every file restore.
    6. Delete created entities — **last**, so references held by restored
       entities are valid until every created entity is removed.

    Pure: touches no filesystem or network. ``load_snapshot`` is the injected
    seam that supplies a snapshot by shared id. If a snapshot for a modified
    or deleted entity cannot be loaded, the error propagates — no silent skip.

    Crash-safe/resumable: the action list is always rebuilt from the manifest
    (not from any in-memory state), and the use case makes the execution
    idempotent — a deleted entity whose manifest entry already carries a
    ``restored_shared_id`` (checkpointed after a prior, possibly interrupted,
    re-create) is not re-created a second time, and already-re-uploaded files
    (``FileRef.restored`` / ``DeletedFile.restored``) are skipped. So a resumed
    revert continues from where it left off without duplicate entities or files.
    """
    actions: list[RevertAction] = []

    def in_scope(shared_id: str) -> bool:
        """Whether ``shared_id`` belongs to the requested operation scope."""
        return shared_ids is None or shared_id in shared_ids

    for rewired in manifest.rewired:
        if not in_scope(rewired.entity.shared_id):
            continue
        actions.append(
            RestoreRelationshipAction(
                entity=rewired.entity,
                property_name=rewired.property_name,
                before=rewired.before,
            )
        )

    for modified in manifest.modified:
        if not in_scope(modified.shared_id):
            continue
        snapshot: EntitySnapshot = load_snapshot(modified.shared_id)
        actions.append(RestoreEntityAction(snapshot=snapshot))
        # Publish/permissions are NOT part of the raw write path: Uwazi strips them
        # from POST /api/entities, so a publish/unpublish or grant change needs its
        # own restore call. Ordering is immaterial (separate collection).
        if snapshot.publish_state is not None:
            actions.append(RestorePublishStateAction(shared_id=modified.shared_id, state=snapshot.publish_state))
        # Non-property connection rows (file-cited / text citations) are likewise
        # invisible to the metadata restore; replay the captured set and drop any
        # row the run added. Live row ids come from the injected seam; without it
        # the replay is still exact for the captured rows but cannot delete extras.
        if snapshot.connection_hubs:
            current_rows = load_current_hubs(modified.shared_id, snapshot.language) if load_current_hubs else []
            actions.append(
                RestoreConnectionHubsAction(
                    shared_id=modified.shared_id,
                    language=snapshot.language,
                    rows=list(snapshot.connection_hubs),
                    current_row_ids=rows_to_delete(current_rows, snapshot.connection_hubs),
                )
            )

    deleted_snapshots: dict[str, EntitySnapshot] = {}
    for deleted in manifest.deleted:
        if not in_scope(deleted.shared_id):
            continue
        snap = load_snapshot(deleted.shared_id)
        deleted_snapshots[deleted.shared_id] = snap
        actions.append(RecreateEntityAction(snapshot=snap))
        # A deleted entity took its non-property connection rows with it (Uwazi
        # tears down every hub touching the sharedId). Replay them onto the NEW
        # sharedId once the re-create has minted it — no live rows to delete.
        if snap.connection_hubs:
            actions.append(
                RestoreConnectionHubsAction(
                    shared_id=deleted.shared_id,
                    language=snap.language,
                    rows=list(snap.connection_hubs),
                )
            )

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
    # Only OUT-OF-SCOPE manifest entities are "handled elsewhere": a scoped undo
    # must still re-add the refs an IN-SCOPE entity holds to a re-created source
    # (the merge target typically referenced the entities it absorbed).
    excluded_existing = manifest_ids - (shared_ids or set())
    inbound_targets = extract_inbound_refs_from_existing(deleted_snapshots, deleted_ids, excluded_existing=excluded_existing)
    if recreate_targets or inbound_targets:
        actions.append(
            ReapplyRelationshipRefsAction(
                recreate_targets=recreate_targets,
                inbound_targets=inbound_targets,
            )
        )

    if shared_ids is None and manifest.deleted_files:
        # Documents first (the primary PDF lands before attachments), then
        # uploaded attachments — the same order as build_file_restore_actions.
        # sorted() is stable, so the manifest's insertion order survives within
        # each kind.
        ordered = sorted(manifest.deleted_files, key=lambda f: 0 if f.kind == "document" else 1)
        actions.append(RestoreDeletedFilesAction(files=list(ordered)))

    for created in manifest.created:
        if not in_scope(created.shared_id):
            continue
        actions.append(DeleteEntityAction(shared_id=created.shared_id))

    return actions
