from abc import ABC, abstractmethod

from uwazi_property_filler.domain.suggestion import Suggestion


class SuggestionStorePort(ABC):
    @abstractmethod
    async def save_suggestions(self, instance_key: str, shared_id: str, suggestions: list[Suggestion]) -> None: ...

    @abstractmethod
    async def list_for(self, instance_key: str, shared_id: str) -> list[Suggestion]: ...

    @abstractmethod
    async def shared_ids(self, instance_key: str) -> set[str]:
        """Shared ids with at least one persisted suggestion."""
