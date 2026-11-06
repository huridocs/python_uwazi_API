from abc import ABC, abstractmethod

from uwazi_property_filler.domain.suggestion import Suggestion


class SuggestionSourcePort(ABC):
    """A read-only source of precomputed suggestions (e.g. a JSONL file)."""

    @abstractmethod
    async def list_for(self, shared_id: str) -> list[Suggestion]: ...

    @abstractmethod
    async def shared_ids(self) -> set[str]:
        """Shared ids that have at least one non-empty suggestion."""
