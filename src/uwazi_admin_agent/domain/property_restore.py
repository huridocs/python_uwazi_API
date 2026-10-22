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

from typing import Any

from uwazi_admin_agent.domain.snapshot import EntityRowSnapshot, EntitySnapshot
from uwazi_admin_agent.domain.suggestion import TITLE_PROPERTY


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


__all__ = [
    "property_restore_payload",
    "row_snapshot_for_language",
    "snapshot_row_raw",
]
