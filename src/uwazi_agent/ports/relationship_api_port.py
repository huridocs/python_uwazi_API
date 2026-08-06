from abc import ABC, abstractmethod

from uwazi_agent.domain.agent_relationship_create import AgentRelationshipCreate
from uwazi_agent.domain.agent_relationship_mutation_result import AgentRelationshipMutationResult


class RelationshipApiPort(ABC):
    @abstractmethod
    async def create_relationships(
        self,
        relationships: list[AgentRelationshipCreate],
        language: str = "en",
    ) -> list[AgentRelationshipMutationResult]: ...
