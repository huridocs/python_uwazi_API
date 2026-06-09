from typing import Optional

from uwazi_agent.domain.agent_thesauri import AgentThesauri
from uwazi_agent.ports.uwazi_api_port import ThesauriApiPort
from uwazi_api.client import UwaziClient


class UwaziApiAdapter(ThesauriApiPort):
    def __init__(self, user: Optional[str] = None, password: Optional[str] = None, url: Optional[str] = None):
        self.client = UwaziClient(user=user, password=password, url=url)
        self._repo = self.client.thesauris

    def get_thesauris(self, language: str) -> list[AgentThesauri]:
        return [AgentThesauri(name=t.name, values=[v.label for v in t.values]) for t in self._repo.get(language)]

    def get_thesauris_by_names(self, names: list[str], language: str) -> list[AgentThesauri]:
        all_by_name = {t.name: t for t in self._repo.get(language)}
        all_by_id = {t.id: t for t in self._repo.get(language)}
        result: list[AgentThesauri] = []
        for name in names:
            found = all_by_name.get(name) or all_by_id.get(name)
            if found is None:
                continue
            result.append(
                AgentThesauri(
                    name=found.name,
                    values=[v.label for v in found.values],
                )
            )
        return result

    def create_thesauri(self, name: str, values: list[str], language: str) -> dict:
        payload = [{"label": label} for label in values]
        return self._repo.create(name=name, values=payload, language=language)

    def update_thesauri(self, name: str, values: list[str], language: str) -> dict:
        existing = self._repo.get(language)
        target = next((t for t in existing if t.name == name), None)
        if target is None:
            raise ValueError(f"Thesauri '{name}' not found")
        existing_map = {v.label: v.id for v in target.values}
        for label in values:
            existing_map.setdefault(label, label)
        return self._repo.add_value(
            thesauri_id=target.id,
            thesauri_name=target.name,
            thesauri_values=existing_map,
            language=language,
        )

    def delete_thesauri(self, name: str, language: str) -> dict:
        existing = self._repo.get(language)
        target = next((t for t in existing if t.name == name), None)
        if target is None:
            raise ValueError(f"Thesauri '{name}' not found")
        return self._repo.delete_unassigned(thesauri_id=target.id, language=language)
