from abc import ABC, abstractmethod

from uwazi_agent.domain.agent_relationship_type import AgentRelationshipType


class RelationshipTypeApiPort(ABC):
    @abstractmethod
    async def get_relationship_types(self) -> list[AgentRelationshipType]: ...

    @abstractmethod
    async def get_relationship_type_names(self) -> list[str]: ...

    @abstractmethod
    async def create_relationship_type(self, name: str, language: str) -> dict: ...

    @abstractmethod
    async def update_relationship_type(self, name: str, new_name: str, language: str) -> dict: ...

    @abstractmethod
    async def delete_relationship_type(self, name: str, language: str) -> dict: ...
