from abc import ABC, abstractmethod

from uwazi_agent.domain.agent_entity import AgentEntity
from uwazi_agent.domain.agent_entity_create import AgentEntityCreate
from uwazi_agent.domain.agent_entity_mutation_result import AgentEntityMutationResult
from uwazi_agent.domain.agent_entity_search_result import AgentEntitySearchResult


class EntityApiPort(ABC):
    @abstractmethod
    async def create_entities(self, entities: list[AgentEntityCreate], language: str) -> list[AgentEntityMutationResult]: ...

    @abstractmethod
    async def get_entities_by_shared_ids(
        self, shared_ids: list[str], language: str, limit: int = 10000
    ) -> list[AgentEntity]: ...

    @abstractmethod
    async def search_entities_by_text(
        self,
        search_term: str,
        template_name: str | None,
        language: str,
        limit: int,
    ) -> AgentEntitySearchResult: ...

    @abstractmethod
    async def get_entities_by_template(
        self,
        template_name: str,
        language: str,
        limit: int,
    ) -> AgentEntitySearchResult: ...

    @abstractmethod
    async def update_entities(self, updates: list[AgentEntity], language: str) -> list[AgentEntityMutationResult]: ...

    @abstractmethod
    async def delete_entities_by_shared_ids(self, shared_ids: list[str]) -> list[AgentEntityMutationResult]: ...
