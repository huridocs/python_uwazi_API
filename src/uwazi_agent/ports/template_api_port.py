from abc import ABC, abstractmethod

from uwazi_agent.domain.agent_template import AgentTemplate


class TemplateApiPort(ABC):
    @abstractmethod
    async def get_templates(self) -> list[AgentTemplate]: ...

    @abstractmethod
    async def get_templates_by_names(self, names: list[str]) -> list[AgentTemplate]: ...

    @abstractmethod
    async def get_template_names(self) -> list[str]: ...

    @abstractmethod
    async def create_template(self, template: AgentTemplate, language: str) -> dict: ...

    @abstractmethod
    async def update_template(self, template: AgentTemplate, language: str) -> dict: ...

    @abstractmethod
    async def delete_template(self, name: str, language: str) -> dict: ...
