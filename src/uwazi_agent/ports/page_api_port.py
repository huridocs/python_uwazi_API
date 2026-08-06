from abc import ABC, abstractmethod

from uwazi_agent.domain.agent_page import AgentPage
from uwazi_agent.domain.agent_page_create import AgentPageCreate
from uwazi_agent.domain.agent_page_mutation_result import AgentPageMutationResult
from uwazi_agent.domain.agent_page_summary import AgentPageSummary
from uwazi_agent.domain.agent_page_update import AgentPageUpdate


class PageApiPort(ABC):
    @abstractmethod
    async def list_pages(self, language: str) -> list[AgentPageSummary]: ...

    @abstractmethod
    async def get_pages_by_shared_ids(self, shared_ids: list[str], language: str) -> list[AgentPage]: ...

    @abstractmethod
    async def create_pages(self, pages: list[AgentPageCreate], language: str) -> list[AgentPageMutationResult]: ...

    @abstractmethod
    async def update_pages(self, updates: list[AgentPageUpdate], language: str) -> list[AgentPageMutationResult]: ...

    @abstractmethod
    async def delete_pages_by_shared_ids(self, shared_ids: list[str], language: str) -> list[AgentPageMutationResult]: ...
