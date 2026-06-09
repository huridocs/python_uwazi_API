from abc import ABC, abstractmethod

from uwazi_agent.domain.agent_thesauri import AgentThesauri
from uwazi_api.domain.thesauri import Thesauri


class ThesauriMapperPort(ABC):
    @abstractmethod
    def to_agent(self, api_thesauri: Thesauri) -> AgentThesauri: ...

    @abstractmethod
    def to_api(self, agent_thesauri: AgentThesauri) -> Thesauri: ...

    @abstractmethod
    def api_values_to_create_payload(self, labels: list[str]) -> list[dict]: ...

    @abstractmethod
    def labels_to_api_values(self, labels: list[str]) -> dict[str, str]: ...
