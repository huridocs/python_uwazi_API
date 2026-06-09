from abc import ABC, abstractmethod

from uwazi_agent.domain.agent_thesauri import AgentThesauri


class ThesauriApiPort(ABC):
    @abstractmethod
    async def get_thesauris(self, language: str) -> list[AgentThesauri]:
        pass

    @abstractmethod
    async def get_thesauris_by_names(self, names: list[str], language: str) -> list[AgentThesauri]:
        pass

    @abstractmethod
    async def create_thesauri(self, name: str, values: list[str], language: str) -> dict:
        pass

    @abstractmethod
    async def update_thesauri(self, name: str, values: list[str], language: str) -> dict:
        pass

    @abstractmethod
    async def delete_thesauri(self, name: str, language: str) -> dict:
        pass
