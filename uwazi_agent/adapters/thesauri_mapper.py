import uuid

from uwazi_agent.domain.agent_thesauri import AgentThesauri
from uwazi_agent.ports.mapper_port import ThesauriMapperPort
from uwazi_api.domain.thesauri import Thesauri
from uwazi_api.domain.thesauri_value import ThesauriValue


class ThesauriMapperAdapter(ThesauriMapperPort):
    def to_agent(self, api_thesauri: Thesauri) -> AgentThesauri:
        return AgentThesauri(
            name=api_thesauri.name,
            values=[v.label for v in api_thesauri.values],
        )

    def to_api(self, agent_thesauri: AgentThesauri) -> Thesauri:
        return Thesauri(
            _id="",
            name=agent_thesauri.name,
            values=[ThesauriValue(label=label, id=self._generate_id()) for label in agent_thesauri.values],
        )

    def api_values_to_create_payload(self, labels: list[str]) -> list[dict]:
        return [{"label": label} for label in labels]

    def labels_to_api_values(self, labels: list[str]) -> dict[str, str]:
        return {label: self._generate_id() for label in labels}

    @staticmethod
    def _generate_id() -> str:
        return uuid.uuid4().hex
