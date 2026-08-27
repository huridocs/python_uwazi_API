"""Pure transform from a snapshot raw entity to a POST /api/entities create payload.

Delete-revert re-creates a deleted entity via the **create branch** of
``POST /api/entities``: when the posted body has no ``sharedId``, Uwazi mints a
fresh ``sharedId``/``_id`` (the update branch, used for modified entities,
requires the original ``_id``+``sharedId`` and 422s on a deleted row). The create
branch's Zod schema (``CreateEntitySchema``) only accepts a fixed set of data
fields and strips the rest, so to keep the re-created entity's data as close to
the original as possible we preserve exactly those accepted fields and drop
identity + server-managed + unsupported fields.

Accepted by create (max data fidelity for what create can restore):
``title``, ``template``, ``icon``, ``user``, ``metadata``, ``attachments``.

Dropped (cannot be restored via the create path — known limitations):
``_id``,
``sharedId`` (re-minted by Uwazi), ``language`` (create takes the
request locale), ``relations`` (the denormalized read view — not writable via
``POST /api/entities``; the underlying `connections` rows are torn down on
delete and re-created separately by ``ReapplyRelationshipRefsAction`` for mutual-
deleted hubs), ``documents``/``file`` (uploaded files — re-uploaded by
``_restore_files``), ``generatedToc``, ``propertySelections``, ``published``
(new entity starts in Uwazi's default publish state), ``creationDate``/``editDate``
(server-managed). In-metadata ``relationship``-property refs to **co-deleted/self**
entities are stripped from ``metadata`` before create (see
:func:`strip_deleted_entity_refs`) so the create branch's existence validation
does not 400; refs to still-existing entities are preserved.

Pure: no I/O; the unit-test target for the delete-revert payload transform.
"""

from __future__ import annotations

from typing import Any

# Fields the POST /api/entities create branch accepts (CreateEntitySchema).
# Keep these from the snapshot raw so the re-created entity carries the
# original data; everything else is dropped (identity is re-minted by Uwazi,
# and relations/documents/etc. cannot be restored via create).
_CREATE_ALLOWED_FIELDS: frozenset[str] = frozenset({"title", "template", "icon", "user", "metadata", "attachments"})


def strip_deleted_entity_refs(metadata: dict[str, Any], deleted_ids: set[str]) -> dict[str, Any]:
    """Drop metadata relationship-property refs to deleted/co-deleted entities.

    Delete-revert re-creates a deleted entity via the POST /api/entities create
    branch, which validates that ``relationship``-type metadata values reference
    **existing** entities (``validateRelationshipForeignIds`` → 400 "references
    non-existent entities"). A ref to the re-created entity itself (self, by its
    OLD sharedId) or to a co-deleted entity points at a not-yet-existing entity,
    so the create would 400. This strips those refs from the create payload so
    re-create succeeds; the mutual relationship is then re-applied separately by
    ``ReapplyRelationshipRefsAction`` (which re-saves the re-created entity with
    its snapshot metadata remapped old→new; the entity-save path's
    ``saveEntityBasedReferences`` (``updateEntities=false``) rebuilds the hub and
    re-populates the ref with the NEW sharedId on success — no self-refs).

    For each metadata property whose value is a non-empty list of ``{value, ...}``
    dicts, drops the entries whose ``value`` is in ``deleted_ids``. Thesaurus /
    select values (UUIDs, never in ``deleted_ids``), scalars, and non-list values
    are untouched; cross-entity refs to entities that **still exist** are
    PRESERVED (those hubs are auto-re-created by ``saveEntityBasedReferences`` on
    create). Pure: returns a new dict; the source ``metadata`` is not mutated.
    """
    stripped: dict[str, Any] = {}
    for prop, value in metadata.items():
        if isinstance(value, list) and value and all(isinstance(v, dict) and "value" in v for v in value):
            kept = [v for v in value if str(v.get("value")) not in deleted_ids]
            stripped[prop] = kept
        else:
            stripped[prop] = value
    return stripped


def to_create_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """Build a ``POST /api/entities`` create payload from a snapshot raw entity.

    Keeps only the data fields the create branch accepts (max fidelity for what
    create can restore) and drops identity + server-managed + unsupported fields.
    A deleted entity re-created from this payload gets a fresh ``sharedId``/``_id``;
    its title, template, icon, user, metadata and url-attachments are restored.

    Pure: returns a new dict; the kept values are referenced (not deep-copied) —
    safe because the payload is JSON-serialized on POST and never mutated here.
    """
    return {key: raw[key] for key in _CREATE_ALLOWED_FIELDS if key in raw}
