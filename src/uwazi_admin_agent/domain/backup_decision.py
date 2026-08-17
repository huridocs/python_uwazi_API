"""Pure backup-intercept decision logic (§2.4, Phase 4).

With a free-form script the touch set is **emergent** — it can't be computed
ahead of time. So the real-execution CRUD helpers are decorated: each mutating
call decides what to back up *before* applying. This module holds the **pure**
pieces of that decision — no I/O, no ports — so they are the primary unit-test
target named by the Phase 4 DoD ("the intercept→snapshot decision, manifest
population").

**First-touch semantics** (the key invariant): only the *first* mutating
operation on an entity snapshots its raw before-state. This closes the
temporal-ordering vulnerability — if a script creates a relationship on entity
X then updates X, the update would snapshot the post-relationship state
(wrong before-state) without first-touch. With first-touch, whichever operation
reaches X first captures the true pre-run state; subsequent operations skip
re-snapshotting.

``decide_backup`` computes *what* to do; ``populate_manifest`` applies it to the
manifest; ``build_rewired_relationships`` extracts relationship before-states
from fetched raws. The intercept class (``use_cases/backup_intercept.py``) calls
these, fetches the raws, saves snapshots, and delegates to the underlying CRUD
helper.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from uwazi_admin_agent.domain.manifest import MigrationManifest, RewiredRelationship
from uwazi_admin_agent.domain.snapshot import EntityIdentity, EntitySnapshot

OpKind = Literal["create", "update", "delete", "create_relationships"]


class BackupDecision(BaseModel):
    """What the intercept should do before applying a mutating CRUD call.

    ``snapshot_ids`` are the entities whose raw before-state must be fetched
    and persisted (first-touch only). ``add_modified``/``add_deleted``/
    ``add_created``/``remove_from_created`` are the manifest category updates
    that ``populate_manifest`` applies. All id lists are subsets of (or
    post-apply results for ``add_created``) the operation's shared_ids.
    """

    model_config = ConfigDict(frozen=True)

    snapshot_ids: list[str] = Field(default_factory=list, description="Entities to fetch raw + snapshot (first-touch).")
    add_modified: list[str] = Field(default_factory=list, description="Add to manifest.modified.")
    add_deleted: list[str] = Field(default_factory=list, description="Add to manifest.deleted.")
    add_created: list[str] = Field(default_factory=list, description="Add to manifest.created (post-create).")
    remove_from_created: list[str] = Field(default_factory=list, description="Remove from manifest.created.")


def decide_backup(
    op_kind: OpKind,
    shared_ids: list[str],
    created: set[str],
    backed_up: set[str],
) -> BackupDecision:
    """Pure: decide what to snapshot and which manifest categories to update.

    ``created`` is the set of shared_ids already in ``manifest.created``
    (script-created entities — no before-state to snapshot). ``backed_up`` is
    the set of entities already snapshotted by a prior first-touch operation.
    Both are passed in as plain sets so the function is pure and testable with
    literals.

    For ``create``: returns an empty decision — shared_ids are assigned by
    Uwazi post-create, so the intercept records ``add_created`` after applying.
    For ``update``/``create_relationships``: first-touch entities not in
    ``created`` are snapshotted and added to ``modified``.
    For ``delete``: entities in ``created`` are removed from ``created``
    (created-then-deleted = nothing to revert); first-touch entities not in
    ``created`` or ``backed_up`` are snapshotted and added to ``deleted``.
    """
    if op_kind == "create":
        return BackupDecision()

    if op_kind in ("update", "create_relationships"):
        snapshot_ids = [sid for sid in shared_ids if sid not in created and sid not in backed_up]
        return BackupDecision(
            snapshot_ids=snapshot_ids,
            add_modified=list(snapshot_ids),
        )

    if op_kind == "delete":
        remove_from_created = [sid for sid in shared_ids if sid in created]
        snapshot_ids = [sid for sid in shared_ids if sid not in created and sid not in backed_up]
        return BackupDecision(
            snapshot_ids=snapshot_ids,
            add_deleted=snapshot_ids,
            remove_from_created=remove_from_created,
        )

    return BackupDecision()  # unreachable: OpKind is exhaustive


def populate_manifest(
    manifest: MigrationManifest,
    decision: BackupDecision,
    snapshots: dict[str, EntitySnapshot],
) -> MigrationManifest:
    """Pure: apply a :class:`BackupDecision` to the manifest (mutates + returns).

    ``snapshots`` keys the :class:`EntitySnapshot` objects already fetched and
    saved by the intercept (keyed by shared_id). For ``add_modified``/
    ``add_deleted``, ``EntityIdentity`` is built from the snapshot's identity
    fields. For ``add_created``, a minimal ``EntityIdentity(shared_id=sid)``
    suffices — the revert of a created entity is a ``DeleteEntityAction`` which
    only needs the shared_id. ``remove_from_created`` filters
    ``manifest.created`` in place.
    """
    for sid in decision.add_modified:
        snap = snapshots[sid]
        manifest.modified.append(
            EntityIdentity(shared_id=snap.shared_id, internal_id=snap.internal_id, language=snap.language)
        )

    for sid in decision.add_deleted:
        snap = snapshots[sid]
        manifest.deleted.append(
            EntityIdentity(shared_id=snap.shared_id, internal_id=snap.internal_id, language=snap.language)
        )

    for sid in decision.add_created:
        manifest.created.append(EntityIdentity(shared_id=sid))

    if decision.remove_from_created:
        remove_set = set(decision.remove_from_created)
        manifest.created = [e for e in manifest.created if e.shared_id not in remove_set]

    return manifest


def build_rewired_relationships(
    from_ids: list[str],
    raws: dict[str, dict[str, Any]],
    language: str,
) -> list[RewiredRelationship]:
    """Pure: build :class:`RewiredRelationship` entries from fetched before-raws.

    Only called for from-entities being snapshotted for the first time (the
    ``decision.snapshot_ids`` for a ``create_relationships`` call). Entities
    already backed up are skipped — their snapshot's ``RestoreEntityAction``
    restores the original relations, making a redundant ``RewiredRelationship``
    unnecessary. The ``before`` is the entity's current ``relations`` field
    value, which (at first touch) is the pre-run state.
    """
    return [
        RewiredRelationship(
            entity=EntityIdentity(shared_id=sid, language=language),
            property_name="relations",
            before=raws[sid].get("relations", []),
        )
        for sid in from_ids
        if sid in raws
    ]
