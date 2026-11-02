from abc import ABC, abstractmethod
from typing import Any


class EntityRepositoryPort(ABC):
    """Raw, high-fidelity access to Uwazi entities for backup/restore (§2.5).

    Implementations fetch and save **raw dicts** - never validated models - so
    round-tripping drops no fields. ``get_raw_by_*`` return the **full** raw
    entity, including its ``relations`` (fetched without ``omitRelationships``).

    In v2 the touch set is emergent (CRUD-intercepted backup, §2.4), so this port
    no longer resolves filters to a touch set. Entity discovery for script
    generation uses ``uwazi_agent``'s ``query_entities``; this port only backs up
    and restores raw entities.
    """

    @abstractmethod
    async def get_raw_by_shared_id(self, shared_id: str, language: str | None = None) -> dict[str, Any]:
        """Fetch the full raw entity JSON for a sharedId (optionally a locale row)."""
        ...

    @abstractmethod
    async def get_raw_by_internal_id(self, internal_id: str) -> dict[str, Any]:
        """Fetch the full raw entity JSON by its Uwazi _id."""
        ...

    @abstractmethod
    async def save_raw(self, raw: dict[str, Any]) -> None:
        """Upsert a raw entity dict back via POST /api/entities (see §8 changelog).

        Used to restore a *modified* entity (the update branch requires the
        original ``_id``/``sharedId`` in the raw).
        """
        ...

    async def save_raw_properties(self, payload: dict[str, Any]) -> None:
        """POST a property-scoped payload: write ONLY the metadata keys it names.

        The counterpart of :meth:`save_raw` for a property-scoped snapshot. It
        never clears a key it was not told about, so it is both the cheap path
        (no index wait) and the correct one for a minimal raw: :meth:`save_raw`
        explicitly clears every property the payload omits, which on a
        property-scoped raw would wipe the entity.

        The caller supplies the exact restore set — including ``[]`` entries for
        properties the run created, which is how Uwazi removes a value. The
        implementation MUST also preserve the entity's files: Uwazi's update
        branch deletes every stored file the body does not re-list.

        Concrete (not abstract) so a test double written before property-scoped
        snapshots still instantiates; a repository that does not support it
        raises, and revert falls back to the whole-entity path.
        """
        raise NotImplementedError(f"{type(self).__name__} does not support property-scoped saves")

    async def update_many_properties(
        self,
        shared_ids: list[str],
        metadata: dict[str, Any],
        language: str,
    ) -> list[str]:
        """Apply ONE metadata payload to many entities at once (POST /api/entities/multipleupdate).

        The bulk fast path for a homogeneous batch: every id receives the same
        property values in the same row language. Returns the ids Uwazi reports
        as updated. The endpoint is all-or-nothing per call and cannot set the
        title, so callers must group by template/language/value and fall back to
        the per-entity path when the call is refused.

        Concrete (not abstract) for the same reason as :meth:`save_raw_properties`:
        an adapter without bulk support raises, and the caller falls back.
        """
        raise NotImplementedError(f"{type(self).__name__} does not support bulk property updates")

    @abstractmethod
    async def create_raw(self, raw: dict[str, Any]) -> str:
        """Re-create a *deleted* entity from its snapshot raw via the create branch.

        POSTs a create payload (no ``sharedId``) to /api/entities so Uwazi mints a
        fresh ``sharedId``/``_id`` and restores the entity's DATA fields. Returns
        the newly-minted ``sharedId`` so the revert use case can record it for
        post-revert verification and audit. Identity is intentionally not
        preserved (exact-data revert for deletes).
        """
        ...

    @abstractmethod
    async def delete_by_shared_id(self, shared_id: str) -> None:
        """Delete all language rows of a sharedId (DELETE /api/entities?sharedId=...)."""
        ...
