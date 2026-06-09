from abc import ABC, abstractmethod

from uwazi_agent.domain.agent_template import AgentTemplate
from uwazi_api.domain.template import Template


class TemplateMapperPort(ABC):
    @abstractmethod
    def to_agent(self, api_template: Template) -> AgentTemplate: ...

    @abstractmethod
    def to_api(self, agent_template: AgentTemplate) -> Template: ...
