"""Restore ONE property of ONE entity-language row from its backup (pure).

The review table's undo used to be entity-wide: a single backup per entity, and
a revert re-wrote every language row from it. The table's suboperations make a
finer undo meaningful — "undo this property value I just wrote" — and that is a
different operation from restoring the entity: take the LIVE raw, replace
exactly one key with the value the snapshot captured, and write it back.

Everything here is a pure dict transformation, so the rule is unit-testable
without Uwazi: the tests pass literal raws in and assert the payload out.
"""

from __future__ import annotations

import json
from typing import Any

from uwazi_admin_agent.domain.snapshot import EntityRowSnapshot, EntitySnapshot
from uwazi_admin_agent.domain.suggestion import TITLE_PROPERTY


def restore_row_language(snapshot: EntitySnapshot, language: str | None, default_language: str = "en") -> str:
    """The entity-language ROW a restore must write for one suboperation (pure).

    A language-independent suboperation (a title, a select, a date) has no
    language of its own: the apply path falls back to the run's DEFAULT language
    row, so its undo has to target that same row. The backup knows it — it is
    the row the snapshot was fetched in (``snapshot.language``) — and looking
    for a language-less row instead would find nothing, because Uwazi never
    stores one. ``default_language`` is only the last resort (a legacy snapshot
    without a primary language), and matches the write path's own fallback.
    """
    return language or snapshot.language or default_language


def snapshot_row_raw(snapshot: EntitySnapshot, language: str | None) -> dict[str, Any] | None:
    """The snapshot raw for one language row, or None when the backup lacks it (pure).

    A modern snapshot carries ``rows`` — one per configured language captured at
    first touch. A legacy snapshot (captured by a build before ``rows``) carries
    a single ``raw`` in the language the backup was fetched in; it stands in for
    that language only, because it genuinely describes no other row. An
    unknown/absent row means there is nothing to restore for the requested
    language, which the caller reports instead of writing a wrong-language value.
    """
    wanted = language or ""
    for row in snapshot.rows:
        if (row.language or "") == wanted:
            return row.raw
    if snapshot.rows:
        return None
    if (snapshot.language or "") == wanted:
        return snapshot.raw
    return None


def property_restore_payload(
    live_raw: dict[str, Any],
    snapshot_raw: dict[str, Any],
    property_name: str,
) -> dict[str, Any]:
    """One live entity raw with exactly one property put back to its backup value (pure).

    The live raw is the base on purpose: the run's OTHER writes to this entity
    (another language row, another property) must survive a single-property
    undo, so nothing is taken from the snapshot except the one value. The value
    keeps Uwazi's stored shape — it is copied from the snapshot's ``metadata``
    verbatim, never re-shaped through the template's property type.

    A property the snapshot does NOT carry did not exist before the run, so
    restoring it means REMOVING the key from the live metadata (rather than
    writing an empty value, which is a statement about the property rather than
    an absence). ``_title`` is the review table's synthetic name for the entity
    title, stored at the raw's top level.
    """
    payload = dict(live_raw)
    if property_name == TITLE_PROPERTY:
        if "title" in snapshot_raw:
            payload["title"] = snapshot_raw["title"]
        else:
            payload.pop("title", None)
        return payload
    live_metadata = dict(live_raw.get("metadata") or {})
    snapshot_metadata = snapshot_raw.get("metadata") or {}
    if property_name in snapshot_metadata:
        live_metadata[property_name] = snapshot_metadata[property_name]
    else:
        live_metadata.pop(property_name, None)
    payload["metadata"] = live_metadata
    return payload


def row_snapshot_for_language(snapshot: EntitySnapshot, language: str | None) -> EntityRowSnapshot | None:
    """The ``EntityRowSnapshot`` for one language, or None (legacy fallback) (pure)."""
    wanted = language or ""
    for row in snapshot.rows:
        if (row.language or "") == wanted:
            return row
    return None


def minimal_property_row(
    raw: dict[str, Any],
    language: str,
    touched: list[str],
) -> EntityRowSnapshot | None:
    """One property-scoped row: identity + title + ONLY the touched keys' before-values (pure).

    Returns ``None`` when the raw cannot support a property-scoped restore —
    Uwazi's update branch requires ``_id`` and a non-empty ``title``, and a
    snapshot that cannot supply them must fall back to the full-entity shape
    rather than write a payload the server would reject.

    ``touched`` is the exact set of metadata keys the run is about to write. A
    key absent from the raw is still recorded in ``touched_properties`` and
    simply left out of the captured metadata: that absence IS the before-state,
    and the restore turns it into an explicit ``[]`` clear.

    The raw is deliberately minimal — no relations, no permissions, no
    documents/attachments, no untouched properties, no other language rows.
    That is what turns a 30 KB snapshot into ~1 KB for a bulk property edit.
    The one non-restore field is ``template``: the bulk write endpoint takes its
    template from the first id in the batch, so the writer needs the template to
    group ids that may share one call. It is never sent back.
    """
    internal_id = raw.get("_id")
    title = raw.get("title")
    if not isinstance(internal_id, str) or not internal_id:
        return None
    if not isinstance(title, str) or not title:
        return None
    row_language = raw.get("language") if isinstance(raw.get("language"), str) else language
    row_language = row_language or language
    source_metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    captured = {key: source_metadata[key] for key in touched if key in source_metadata}
    return EntityRowSnapshot(
        language=row_language,
        internal_id=internal_id,
        raw={
            "_id": internal_id,
            "sharedId": raw.get("sharedId"),
            "language": row_language,
            "title": title,
            "template": raw.get("template"),
            "metadata": captured,
        },
        touched_properties=list(dict.fromkeys(touched)),
    )


def merge_touched_properties(
    row: EntityRowSnapshot,
    live_raw: dict[str, Any],
    touched: list[str],
) -> EntityRowSnapshot:
    """Extend an existing property-scoped row with keys it has not captured yet (pure).

    A run accumulates: a second Run against the same manifest writes more
    properties onto an entity that already has a property-scoped snapshot. The
    first captured value is the pre-ANY-write truth and must survive, so keys
    already recorded are left alone; a NEW key takes its value from the live
    raw, which is untouched for that key (the run has not written it yet).
    """
    known = set(row.touched_properties)
    added = [key for key in touched if key not in known]
    if not added:
        return row
    metadata = dict(row.raw.get("metadata") or {})
    live_metadata = live_raw.get("metadata") if isinstance(live_raw.get("metadata"), dict) else {}
    for key in added:
        if key in live_metadata:
            metadata[key] = live_metadata[key]
    return row.model_copy(
        update={
            "raw": {**row.raw, "metadata": metadata},
            "touched_properties": [*row.touched_properties, *added],
        }
    )


def bulk_property_groups(
    plans: list[dict[str, Any]],
    min_group: int = 1,
) -> list[list[dict[str, Any]]]:
    """Partition write plans into homogeneous groups a bulk call can take (pure).

    ``POST /api/entities/multipleupdate`` applies ONE ``values.metadata`` to many
    ids in ONE row language. It CANNOT set the title and it takes the template
    from the first id in the batch, so a group is bulkable only when every plan
    shares the language, the template and the exact same metadata payload, and
    none of them changes the title. Anything else stays a singleton and takes the
    ordinary per-entity POST.

    ``min_group`` defaults to 1: a bulk call for a single id is not worse than
    the per-entity POST (one request either way) and it is strictly SAFER,
    because the bulk endpoint never touches the entity's files while the POST
    update branch removes every file the payload does not re-list. The parameter
    stays for callers that explicitly want a different cut-off.

    Order is not significant — callers key the results by ``shared_id``.
    """
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    singles: list[dict[str, Any]] = []
    for plan in plans:
        key = _bulk_key(plan)
        if key is None:
            singles.append(plan)
            continue
        groups.setdefault(key, []).append(plan)
    result: list[list[dict[str, Any]]] = []
    for group in groups.values():
        if len(group) >= min_group:
            result.append(group)
        else:
            result.extend([plan] for plan in group)
    result.extend([plan] for plan in singles)
    return result


def can_bulk_update(plan: dict[str, Any]) -> bool:
    """Whether ONE write plan may ride ``POST /api/entities/multipleupdate`` (pure).

    A title write cannot (the endpoint has no title input), and a plan without a
    template, a language or any metadata has nothing the endpoint could apply.
    """
    return _bulk_key(plan) is not None


def _bulk_key(plan: dict[str, Any]) -> tuple[str, str, str] | None:
    """The grouping key of a plan a bulk call can carry, or None when it cannot (pure)."""
    if plan.get("title_changed"):
        return None
    template = plan.get("template")
    language = plan.get("language")
    metadata = plan.get("metadata")
    if not template or not language or not metadata:
        return None
    # A canonical rendering: dict order must not split an otherwise identical batch.
    fingerprint = json.dumps(metadata, sort_keys=True, default=str)
    return (str(language), str(template), fingerprint)


def keep_file_entries(raw: dict[str, Any] | None) -> dict[str, list[dict[str, str]]]:
    """The ``documents``/``attachments`` lists a property update must ECHO BACK (pure).

    Uwazi's update branch treats the payload's file lists as the complete set:
    every stored file whose id is not among them is DELETED. A property-scoped
    body carries no files, so posting one as-is would strip the entity of its
    documents, attachments and preview. This builds the keep-list from the
    entity's current raw — ``_id`` + ``originalname`` are what the mapper needs,
    and a document without an ``_id`` cannot be kept (it would be a new upload).
    """
    source = raw if isinstance(raw, dict) else {}
    documents = [
        {"_id": entry["_id"], "originalname": entry["originalname"]}
        for entry in source.get("documents") or []
        if isinstance(entry, dict)
        and isinstance(entry.get("_id"), str)
        and entry["_id"]
        and isinstance(entry.get("originalname"), str)
    ]
    attachments: list[dict[str, str]] = []
    for entry in source.get("attachments") or []:
        if not isinstance(entry, dict) or not isinstance(entry.get("originalname"), str):
            continue
        kept = {"originalname": entry["originalname"]}
        if isinstance(entry.get("_id"), str) and entry["_id"]:
            kept["_id"] = entry["_id"]
        attachments.append(kept)
    return {"documents": documents, "attachments": attachments}


def snapshot_restore_payload(shared_id: str, row: EntityRowSnapshot) -> dict[str, Any]:
    """The ``POST /api/entities`` body that puts ONE property-scoped row back (pure).

    Carries exactly the keys :func:`minimal_property_row` captured: ``_id``,
    ``sharedId``, ``language`` and ``title`` (all required by Uwazi's update
    schema) plus one metadata entry per touched key. A key the run CREATED is
    sent as ``[]`` — Uwazi merges metadata at the property level, so an empty
    list is how a value is removed, and it is the only way to undo an addition.

    Every other property is absent from the body on purpose: the server-side
    merge leaves it untouched, which is what makes a property-scoped revert
    surgical instead of a whole-entity rewrite.
    """
    metadata = row.raw.get("metadata") if isinstance(row.raw.get("metadata"), dict) else {}
    return {
        "_id": row.internal_id or row.raw.get("_id"),
        "sharedId": shared_id,
        "language": row.language or row.raw.get("language") or "en",
        "title": row.raw.get("title") or "",
        "metadata": {key: metadata[key] if metadata.get(key) is not None else [] for key in row.touched_properties},
    }


__all__ = [
    "bulk_property_groups",
    "can_bulk_update",
    "keep_file_entries",
    "merge_touched_properties",
    "minimal_property_row",
    "property_restore_payload",
    "restore_row_language",
    "row_snapshot_for_language",
    "snapshot_restore_payload",
    "snapshot_row_raw",
]
