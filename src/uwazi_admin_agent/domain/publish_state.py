"""The publish/permission state of one entity row, captured for exact revert.

Why this exists: ``POST /api/entities`` is the restore path for a modified
entity, but Uwazi does not write ``published`` or ``permissions`` from that
payload (``MongoEntitiesDataSource.bulkUpdate`` / ``PostgresEntitiesDataSource
.toUpdateRow`` strip both; the access policy lives in its own collection). A
run that published or unpublished an entity therefore left no revertable
trace: the raw round-trip preserved *content* only, and a revert reported
success while the public URL stayed changed.

Publishing in Uwazi IS the presence of a ``public`` permission grant
(``EntityPermissionsController`` derives ``isPublic`` from the posted
permission list), so capturing the grants captures publishing too. The
``published`` flag is kept alongside for comparison/verification.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PublishState(BaseModel):
    """One entity's permission grants at backup time (the revert target).

    ``permissions`` are Uwazi's raw permission rows
    (``{"refId", "type": "user"|"group"|"public", "level": "read"|"write"}``),
    captured from ``GET /api/entities?include=permissions``. A ``public`` entry
    means the entity was published; its absence means it was not.
    """

    model_config = ConfigDict(frozen=True)

    published: bool = Field(description="The row's denormalized published flag at capture time.")
    permissions: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Raw permission grants (including the public entry when published).",
    )


def public_entry_present(permissions: list[dict[str, Any]]) -> bool:
    """True when the grants include Uwazi's public read grant (pure).

    Mirrors the server's own test (``refId == "public" and type == "public"``)
    used by ``publish_entities``/``unpublish_entities``.
    """
    return any(p.get("refId") == "public" and p.get("type") == "public" for p in permissions)


def is_restored(state: PublishState, current: PublishState) -> bool:
    """True when ``current`` already carries the captured publish state (pure).

    Grants are compared as a set of ``(refId, type, level)`` triples so server
    re-ordering is not a false mismatch. Levels count: a read-only grant
    upgraded to write is a real difference.
    """
    if state.published != current.published:
        return False
    return _grant_keys(state.permissions) == _grant_keys(current.permissions)


def _grant_keys(permissions: list[dict[str, Any]]) -> set[tuple[str, str, str]]:
    return {
        (str(p.get("refId")), str(p.get("type")), str(p.get("level"))) for p in permissions if p.get("refId") is not None
    }
