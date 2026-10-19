"""Pure helpers for capturing + restoring connection hubs the metadata path cannot rebuild.

The gap this closes: Uwazi relationship hubs live in the ``connections``
collection, two rows per hub (a FROM row with ``template: null`` and a TO row
carrying the relation-type id). ``POST /api/entities`` rebuilds only the hubs a
relationship *property* implies, from the posted metadata. A hub whose FROM row
carries ``file`` (a file-cited connection) or ``reference`` (a text citation)
belongs to no property — the metadata restore never recreates it, so a run that
created one could not undo it and a revert left it live. Deleting an entity
destroys every hub touching it, and only the property hubs come back.

So hubs are captured at first touch (:attr:`EntitySnapshot.connection_hubs`,
from the ``relations`` view already present in the raw) and restored through
``POST /api/relationships/bulk`` — the endpoint Uwazi's own reference save uses.

**Only non-reproducible hubs are captured.** A hub is reproducible when every
row has neither ``file`` nor ``reference`` and exactly one row is typed: that is
precisely the shape ``saveEntityBasedReferences`` rebuilds from metadata, and
replaying it through the bulk endpoint would re-derive metadata (the self-ref
hazard documented in :mod:`domain.revert`). Capturing it would add risk, not
fidelity.

**Hub granularity matters.** Restoring a single row of a citation hub produces
a broken half-hub (the server's ``prepareRelationshipsToSave`` refuses or the
read view drops it via ``removeSingleHubs``), so capture and restore work on
whole hubs.
"""

from __future__ import annotations

from typing import Any

# Hub row keys no metadata restore can reproduce.
_UNREPRODUCIBLE_KEYS: tuple[str, ...] = ("file", "reference")

# Keys carried through to the bulk endpoint. ``entityData``/``label`` are read
# views the server re-derives, ``__v`` is bookkeeping, and ``_id``/``hub``
# describe the OLD rows (a restored hub is a NEW hub; Uwazi mints its own ids).
_DROPPED_ROW_KEYS: frozenset[str] = frozenset({"_id", "__v", "entityData", "label", "hub"})

HubSignature = tuple[tuple[str, str, str, str], ...]


def hub_signature(rows: list[dict[str, Any]]) -> HubSignature:
    """Pure: an identity for a hub that survives re-minting (hub ids are new).

    Built from the sorted row signatures ``(entity, template, file,
    reference-text)`` inside the hub. Two hubs with the same endpoints, type and
    citation are the same relationship from the operator's point of view, so
    this is what "already restored" means on a second revert pass.
    """
    return tuple(
        sorted(
            (
                str(row.get("entity") or ""),
                str(row.get("template") or ""),
                str(row.get("file") or ""),
                _reference_text(row),
            )
            for row in rows
        )
    )


def is_reproducible_hub(rows: list[dict[str, Any]]) -> bool:
    """True when ``POST /api/entities`` rebuilds this hub from metadata (pure).

    Every row must be free of ``file``/``reference``, and exactly one row must
    be typed (the relation-type id) — the shape ``buildRelationshipMetadata``
    derives from a relationship property. Anything else (a citation, a file
    connection, a malformed single-row hub) needs explicit capture.
    """
    if not rows:
        return False
    if any(row.get(key) for row in rows for key in _UNREPRODUCIBLE_KEYS):
        return False
    typed = [row for row in rows if row.get("template")]
    return len(typed) == 1 and len(rows) >= 2


def group_rows_by_hub(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Pure: group connection rows into hubs, first-appearance order."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        hub = row.get("hub")
        grouped.setdefault(str(hub) if hub is not None else "", []).append(row)
    return list(grouped.values())


def extract_connection_hubs(relations: Any) -> list[dict[str, Any]]:
    """Pure: the rows of every non-reproducible hub in an entity's ``relations`` view.

    ``relations`` is the raw entity's ``relations`` array (the denormalized view
    of the entity's hubs, both endpoints included). Returns the rows of each hub
    that the metadata restore cannot rebuild, deduplicated by
    :func:`hub_signature` and stripped of identity/read-view keys.

    Returns ``[]`` for a missing/odd value — an entity with no citations needs
    no capture.
    """
    if not isinstance(relations, list):
        return []
    kept: list[dict[str, Any]] = []
    seen: set[HubSignature] = set()
    for hub_rows in group_rows_by_hub([r for r in relations if isinstance(r, dict) and r.get("entity")]):
        if is_reproducible_hub(hub_rows):
            continue
        signature = hub_signature(hub_rows)
        if signature in seen:
            continue
        seen.add(signature)
        kept.extend({k: v for k, v in row.items() if k not in _DROPPED_ROW_KEYS} for row in hub_rows)
    return kept


def hub_groups(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Pure: group captured rows back into hubs for the bulk ``save`` payload.

    A captured hub's rows are contiguous in manifest order; grouping by their
    original ``hub`` is not possible (the key is stripped), so rows are grouped
    by the FROM row (``template`` falsy): each FROM row starts a hub, and the
    typed rows that follow belong to it. A captured hub always has exactly one
    FROM row, which is what makes this unambiguous.
    """
    groups: list[list[dict[str, Any]]] = []
    for row in rows:
        if not row.get("template") or not groups:
            groups.append([])
        groups[-1].append(row)
    return [g for g in groups if g]


def rows_to_delete(current_rows: list[dict[str, Any]], captured_rows: list[dict[str, Any]]) -> list[str]:
    """Pure: live row ids of non-reproducible hubs the captured state does not contain.

    These are the citation hubs the run ADDED (or whose content changed), so an
    exact restore deletes them before re-saving the captured set. Reproducible
    hubs are never touched here — the metadata restore owns them.
    """
    captured = {hub_signature(group) for group in hub_groups(captured_rows)}
    stale: list[str] = []
    for hub_rows in group_rows_by_hub([r for r in current_rows if isinstance(r, dict) and r.get("entity")]):
        if is_reproducible_hub(hub_rows) or hub_signature(hub_rows) in captured:
            continue
        stale.extend(str(row["_id"]) for row in hub_rows if row.get("_id") is not None)
    return stale


def remap_hub_rows(rows: list[dict[str, Any]], id_map: dict[str, str]) -> list[dict[str, Any]]:
    """Pure: rewrite endpoint sharedIds through the old→new map (delete-revert).

    A re-created entity has a fresh sharedId; a captured row pointing at the old
    one would be dropped by the bulk endpoint's existence check.
    """
    if not id_map:
        return [dict(r) for r in rows]
    remapped: list[dict[str, Any]] = []
    for row in rows:
        copy = dict(row)
        entity = copy.get("entity")
        if isinstance(entity, str) and entity in id_map:
            copy["entity"] = id_map[entity]
        remapped.append(copy)
    return remapped


def _reference_text(row: dict[str, Any]) -> str:
    reference = row.get("reference")
    return str(reference.get("text") or "") if isinstance(reference, dict) else ""
