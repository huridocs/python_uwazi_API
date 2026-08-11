from abc import ABC, abstractmethod
from typing import Any

from uwazi_admin_agent.domain.filter import EntityFilter
from uwazi_admin_agent.domain.snapshot import EntityIdentity


class EntityRepositoryPort(ABC):
    """Raw, high-fidelity access to Uwazi entities (§2.5, §5.3).

    Implementations fetch and save **raw dicts** - never validated models - so
    round-tripping drops no fields. ``get_raw_by_*`` return the **full** raw
    entity, including its relationships (the adapter fetches without
    ``omitRelationships``); a separate relationships fetch is not needed.
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

    @abstractmethod
    async def find_touch_set(self, entity_filter: EntityFilter) -> list[EntityIdentity]:
        """Resolve a filter to the entities it selects (the touch set, pre-mutation).

        Uses /api/search in the adapter (§8: GET /api/entities needs sharedId/_id).
        """
        ...
