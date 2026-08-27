"""Pure transforms for restoring relationship refs on delete-revert.

When a script deletes one or more entities, Uwazi tears down the
``connections`` hubs touching them (``BulkCleanupEntityUseCase`` →
``relationshipsDS.bulkDeleteBySharedId`` + orphan-hub cleanup) **and**
cascade-removes the deleted sharedIds from every other entity's in-metadata
``relationship``-property refs (``entitiesDS.deleteReferencesToSharedIds`` →
``updateMetadataReferences``). Delete-revert re-creates each deleted entity as
A'/B' under **fresh sharedIds** (exact-data, not exact-identity), so neither
the old hubs (referencing the dead OLD sharedIds) nor the cascade-stripped
inbound refs come back on their own. This module is the relationship analogue
of :mod:`domain.file_restore`.

**Mechanism — entity-save-path metadata re-application, NOT the bulk path.**
The restore goes through ``save_raw`` (the POST /api/entities update branch),
whose ``saveEntityBasedReferences`` calls ``relationships.save(...,
updateEntities=false)``: it builds outgoing connection hubs FROM the posted
metadata and **never re-derives metadata**, so no self-refs appear. The bulk
path (``POST /api/relationships/bulk``) forces ``updateEntities=true`` →
``buildRelationshipMetadata`` re-derives metadata WITHOUT excluding
``r.entity === entity.sharedId``, producing self-refs (A'→A') — so it is NOT
used.

**Capture source — the snapshot's existing ``relations`` field.** Unlike file
bytes (destroyed on delete, so they need a separate backup-time capture), the
relationship info is **already in the snapshot raw**: ``get_raw_by_shared_id``
fetches without ``omitRelationships``, so each deleted entity's snapshot carries
its ``relations`` — the denormalized read view of the ``connections`` collection
(``getByDocument`` → ``processRelationshipCollection``). Each relation entry
carries the underlying connection-row fields ``entity`` (endpoint sharedId),
``hub`` (ObjectId), and ``template`` (relation-type id on the TO row, ``null``
on the FROM row) — see ``RelationDTO`` in the Uwazi backend. The admin agent
fetches as an authenticated admin (``includeUnpublished=true``), so the view is
complete. So no separate backup-time fetch is needed; the hub groups are
extracted from the snapshots at revert-build time.

Two restore scopes (both pure-derived from snapshots, no manifest change):
- **Mutual-deleted** (both endpoints ∈ ``manifest.deleted``): after both are
  re-created, each is ``save_raw``-ed with its snapshot metadata remapped
  old→new; ``saveEntityBasedReferences`` rebuilds the outgoing hub. See
  :class:`CapturedHub` / :func:`extract_mutual_deleted_hubs` (also the
  verification target) and :func:`has_co_deleted_refs` (gates the re-save).
- **Inbound from still-existing** (FROM ∈ non-deleted, TO ∈ deleted): the
  still-existing entity's ref was cascade-removed; revert re-adds the remapped
  ref to that entity's metadata and ``save_raw``-s it. See :class:`InboundRef`
  / :func:`extract_inbound_refs_from_existing`. Existing entities that are
  themselves in the manifest (modified/deleted/created) are excluded — that
  stale-id edge case is a documented limitation.

See ``domain/create_payload.py::strip_deleted_entity_refs`` for the prerequisite
that strips co-deleted/self metadata refs so the create branch does not 400.

This module holds the **pure** pieces (no I/O) — the unit-test target.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class CapturedHub(BaseModel):
    """One mutual-deleted relationship hub captured before delete (old ids).

    A Uwazi relationship is a hub grouping 2 rows in the ``connections``
    collection: a FROM row (``template: null``) and a TO row
    (``template: <relation-type id>``). ``from_shared_id``/``to_shared_id`` are
    the OLD sharedIds of those rows; on revert they are remapped to the re-created
    entities' NEW sharedIds via the manifest's old→new id map. ``relation_type``
    ``relation_type`` is the TO row's ``template`` (an ObjectId string); ``None`` should not occur
    for a captured hub (a hub with no typed TO row is not a restorable
    relationship-property connection).
    """

    model_config = ConfigDict(frozen=True)

    hub: str = Field(description="The original hub ObjectId (string) — dedup key + audit trace.")
    from_shared_id: str = Field(description="The FROM row's OLD sharedId (the template:null side).")
    to_shared_id: str = Field(description="The TO row's OLD sharedId (the template:<type> side).")
    relation_type: str = Field(description="The TO row's template — the relation-type ObjectId string.")


class InboundRef(BaseModel):
    """One inbound relationship ref from a still-existing entity to a deleted one.

    Discovered from the deleted snapshots' ``relations``: a hub whose FROM row
    (``template: null``) belongs to a **non-deleted** entity and whose TO row
    (``template: <R>``) belongs to a **deleted** entity. That still-existing
    entity's in-metadata ``relationship``-property ref to the deleted entity was
    cascade-removed on delete (``deleteReferencesToSharedIds``); revert re-adds
    it, remapped to the re-created entity's NEW sharedId, and ``save_raw``-s the
    still-existing entity so ``saveEntityBasedReferences`` rebuilds the hub.

    ``deleted_template_id`` is the deleted (TO) entity's template id — used as
    the ``content`` filter when resolving the property NAME on the existing
    entity's template (mirrors ``buildRelationshipMetadata``'s
    ``(relationType, content)`` match). Pure data carried from build time to the
    use case's execution-time template lookup.
    """

    model_config = ConfigDict(frozen=True)

    existing_shared_id: str = Field(description="The still-existing FROM entity's sharedId.")
    deleted_shared_id: str = Field(description="The deleted TO entity's OLD sharedId (remapped at execution).")
    relation_type: str = Field(description="The TO row's template — the relation-type ObjectId string.")
    deleted_template_id: str | None = Field(
        default=None,
        description="The deleted entity's template id — the property `content` filter for name resolution.",
    )


def _endpoint_ids(rows: list[dict[str, Any]]) -> set[str]:
    """The set of endpoint sharedIds in a hub's relation rows."""
    return {str(r["entity"]) for r in rows if isinstance(r, dict) and r.get("entity") is not None}


def _split_from_to(rows: list[dict[str, Any]]) -> tuple[str | None, str | None, str | None]:
    """Return (from_shared_id, to_shared_id, relation_type) for a hub's rows.

    The FROM row has a falsy ``template`` (null/None/""), the TO row carries the
    relation-type id. If both rows carry a type (unexpected for an entity-to-
    entity property hub) the first typed row is treated as the TO row.
    """
    from_sid: str | None = None
    to_sid: str | None = None
    rel_type: str | None = None
    for row in rows:
        entity = row.get("entity")
        template = row.get("template")
        if template:
            if to_sid is None:
                to_sid = str(entity) if entity is not None else None
                rel_type = str(template)
        else:
            if from_sid is None:
                from_sid = str(entity) if entity is not None else None
    return from_sid, to_sid, rel_type


def extract_mutual_deleted_hubs(
    deleted_snapshots: dict[str, Any],
    deleted_ids: set[str],
) -> list[CapturedHub]:
    """Pure: extract mutual-deleted relationship hubs from deleted snapshots.

    ``deleted_snapshots`` keys the :class:`EntitySnapshot` for each deleted
    entity (loaded via the backup store). Each snapshot's ``raw['relations']``
    is the denormalized view of the connection rows in that entity's hubs (the
    view includes BOTH endpoints' rows per hub). Rows are grouped by ``hub``;
    a hub is kept only if **every** endpoint ``entity`` is in ``deleted_ids``
    (mutual-deleted — the case the create path can't auto-restore). Hubs to
    still-existing entities are dropped (auto-restored via metadata preservation).

    Hubs are deduplicated by ``hub`` id (the same hub appears in both endpoints'
    snapshots). Hubs missing a typed TO row (no ``relation_type``) are dropped
    (not a restorable relationship-property connection). Pure: no I/O; reads
    snapshot.raw without mutating it.
    """
    deleted_ids = {str(sid) for sid in deleted_ids}
    grouped: dict[str, list[dict[str, Any]]] = {}
    origins: dict[str, str] = {}
    for sid, snapshot in deleted_snapshots.items():
        if snapshot is None:
            continue
        raw = getattr(snapshot, "raw", None) if not isinstance(snapshot, dict) else snapshot.get("raw")
        relations = raw.get("relations") if isinstance(raw, dict) else None
        if not isinstance(relations, list):
            continue
        for rel in relations:
            if not isinstance(rel, dict) or rel.get("hub") is None:
                continue
            hub_key = str(rel["hub"])
            grouped.setdefault(hub_key, [])
            # A hub is processed once per snapshot; avoid pushing the same row
            # twice if a snapshot oddly lists duplicates.
            if hub_key not in origins or origins[hub_key] == sid:
                grouped[hub_key].append(rel)
            origins.setdefault(hub_key, sid)

    hubs: list[CapturedHub] = []
    for hub_key, rows in grouped.items():
        endpoints = _endpoint_ids(rows)
        if not endpoints or not endpoints.issubset(deleted_ids):
            continue
        from_sid, to_sid, rel_type = _split_from_to(rows)
        if not rel_type or not from_sid or not to_sid:
            continue
        hubs.append(
            CapturedHub(
                hub=hub_key,
                from_shared_id=from_sid,
                to_shared_id=to_sid,
                relation_type=rel_type,
            )
        )
    return hubs


def build_relationship_recreate_payload(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    """Removed: the bulk ``POST /api/relationships/bulk`` path forces
    ``updateEntities=true`` → ``buildRelationshipMetadata`` re-derives metadata
    without excluding ``r.entity === entity.sharedId``, producing self-refs.
    Relationship restore now goes through the entity-save path
    (``save_raw`` → ``saveEntityBasedReferences`` with ``updateEntities=false``).
    Kept as a loud guard so stale callers fail clearly instead of silently
    dropping relationships.
    """
    raise NotImplementedError(
        "build_relationship_recreate_payload was removed; relationship restore "
        "now uses the entity-save path (reapply metadata + save_raw)."
    )


def has_co_deleted_refs(metadata: dict[str, Any], deleted_ids: set[str]) -> bool:
    """Pure: does ``metadata`` hold a relationship-property ref to a co-deleted entity?

    A deleted entity is re-created with co-deleted/self refs STRIPPED (see
    :func:`strip_deleted_entity_refs`); the mutual hub is then restored by
    re-saving the re-created entity with its snapshot metadata remapped old→new.
    This predicate gates that re-save so a deleted entity whose only refs are to
    still-existing entities (auto-restored on create) is NOT re-saved needlessly.
    True iff some property's value is a non-empty list of ``{value, ...}`` dicts
    with at least one entry whose ``value`` ∈ ``deleted_ids``. Pure.
    """
    deleted_ids = {str(sid) for sid in deleted_ids}
    for value in metadata.values():
        if isinstance(value, list) and value and all(isinstance(v, dict) and "value" in v for v in value):
            if any(str(v.get("value")) in deleted_ids for v in value):
                return True
    return False


def extract_inbound_refs_from_existing(
    deleted_snapshots: dict[str, Any],
    deleted_ids: set[str],
    excluded_existing: set[str] | None = None,
) -> list[InboundRef]:
    """Pure: discover inbound refs from still-existing entities to deleted ones.

    Scans each deleted snapshot's ``raw['relations']``, groups rows by ``hub``,
    and keeps a hub only if its FROM row (``template`` falsy) belongs to a
    non-deleted entity and its TO row (``template: <R>``) belongs to a deleted
    entity — i.e. a still-existing entity had an in-metadata ref to a deleted
    entity, which the delete cascade stripped. The deleted entity's template id
    (the property ``content`` filter for name resolution) is pulled from the
    matching deleted snapshot's ``raw['template']``.

    ``excluded_existing`` (defaults to empty) drops refs whose existing entity is
    itself in the manifest (modified/deleted/created) — those stale-id cases are
    a documented limitation, out of scope for this restore. Dedups by
    ``(existing, deleted, relation_type)`` (the same hub appears in both
    endpoints' snapshots). Pure: reads snapshot.raw without mutating it.
    """
    deleted_ids = {str(sid) for sid in deleted_ids}
    excluded = {str(sid) for sid in (excluded_existing or set())}
    grouped: dict[str, list[dict[str, Any]]] = {}
    for sid, snapshot in deleted_snapshots.items():
        if snapshot is None:
            continue
        raw = getattr(snapshot, "raw", None) if not isinstance(snapshot, dict) else snapshot.get("raw")
        relations = raw.get("relations") if isinstance(raw, dict) else None
        if not isinstance(relations, list):
            continue
        for rel in relations:
            if not isinstance(rel, dict) or rel.get("hub") is None:
                continue
            grouped.setdefault(str(rel["hub"]), []).append(rel)

    refs: list[InboundRef] = []
    seen: set[tuple[str, str, str]] = set()
    for rows in grouped.values():
        from_sid, to_sid, rel_type = _split_from_to(rows)
        if not rel_type or not from_sid or not to_sid:
            continue
        if from_sid in deleted_ids or to_sid not in deleted_ids:
            continue
        if from_sid in excluded:
            continue
        key = (from_sid, to_sid, rel_type)
        if key in seen:
            continue
        seen.add(key)
        deleted_template_id = None
        to_snap = deleted_snapshots.get(to_sid)
        to_raw = getattr(to_snap, "raw", None) if to_snap is not None and not isinstance(to_snap, dict) else None
        if isinstance(to_raw, dict):
            deleted_template_id = to_raw.get("template")
        refs.append(
            InboundRef(
                existing_shared_id=from_sid,
                deleted_shared_id=to_sid,
                relation_type=rel_type,
                deleted_template_id=deleted_template_id if isinstance(deleted_template_id, str) else None,
            )
        )
    return refs


def remap_metadata_refs(metadata: dict[str, Any], id_map: dict[str, str]) -> dict[str, Any]:
    """Pure: remap in-metadata relationship-property refs via the old→new id map.

    For each property whose value is a non-empty list of ``{value, ...}`` dicts,
    replaces entries' ``value`` with ``id_map[value]`` when ``value`` is a key in
    ``id_map`` (a re-created deleted entity); other entries are unchanged.
    Non-list / non-``{value}`` values (thesaurus UUIDs, scalars, links, ...) are
    passed through untouched — only old sharedIds present in ``id_map`` are
    remapped, so refs to still-existing entities stay as-is. Used by
    :mod:`domain.revert_verification` so a re-created entity's metadata ref to a
    co-deleted entity compares equal once the remap is applied to the snapshot.
    Pure: returns a new dict; the source ``metadata`` is not mutated.
    """
    id_map = {str(k): str(v) for k, v in id_map.items()}
    remapped: dict[str, Any] = {}
    for prop, value in metadata.items():
        if isinstance(value, list) and value and all(isinstance(v, dict) and "value" in v for v in value):
            remapped[prop] = [{**v, "value": id_map[str(v["value"])]} if str(v.get("value")) in id_map else v for v in value]
        else:
            remapped[prop] = value
    return remapped
