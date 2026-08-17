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
        """Upsert a raw entity dict back via POST /api/entities (see §8 changelog)."""
        ...

    @abstractmethod
    async def delete_by_shared_id(self, shared_id: str) -> None:
        """Delete all language rows of a sharedId (DELETE /api/entities?sharedId=...)."""
        ...
