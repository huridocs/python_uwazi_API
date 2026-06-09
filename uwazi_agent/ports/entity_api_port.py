from abc import ABC, abstractmethod

from uwazi_agent.domain.agent_entity import (
    AgentEntity,
    AgentEntityMutationResult,
    AgentEntitySearchResult,
)


class EntityApiPort(ABC):
    @abstractmethod
    async def get_entities_by_shared_ids(self, shared_ids: list[str], language: str) -> list[AgentEntity]: ...

    @abstractmethod
    async def search_entities_by_text(
        self,
        search_term: str,
        template_name: str | None,
        language: str,
        limit: int,
    ) -> AgentEntitySearchResult: ...

    @abstractmethod
    async def update_entities(self, updates: list[AgentEntity], language: str) -> list[AgentEntityMutationResult]: ...

    @abstractmethod
    async def delete_entities_by_shared_ids(self, shared_ids: list[str]) -> list[AgentEntityMutationResult]: ...
