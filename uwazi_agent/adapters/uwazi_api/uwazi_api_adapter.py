import asyncio
from typing import Optional

from uwazi_agent.adapters.template_mapper import TemplateMapperAdapter
from uwazi_agent.adapters.uwazi_api.thesaurus_gateway import build_template_mapper_from_client
from uwazi_agent.domain.agent_template import AgentTemplate
from uwazi_agent.domain.agent_thesauri import AgentThesauri
from uwazi_agent.ports.template_api_port import TemplateApiPort
from uwazi_agent.ports.thesauri_api_port import ThesauriApiPort
from uwazi_api.client import UwaziClient


class UwaziApiAdapter(ThesauriApiPort, TemplateApiPort):
    def __init__(
        self,
        user: Optional[str] = None,
        password: Optional[str] = None,
        url: Optional[str] = None,
        template_mapper: Optional[TemplateMapperAdapter] = None,
    ):
        self.client = UwaziClient(user=user, password=password, url=url)
        self._thesauri_repo = self.client.thesauris
        self._template_repo = self.client.templates
        self._template_mapper = template_mapper or build_template_mapper_from_client(self.client)

    @property
    def template_mapper(self) -> TemplateMapperAdapter:
        return self._template_mapper

    async def get_thesauris(self, language: str) -> list[AgentThesauri]:
        def _fetch() -> list[AgentThesauri]:
            return [
                AgentThesauri(name=t.name, values=[v.label for v in t.values]) for t in self._thesauri_repo.get(language)
            ]

        return await asyncio.to_thread(_fetch)

    async def get_thesauris_by_names(self, names: list[str], language: str) -> list[AgentThesauri]:
        def _fetch() -> list[AgentThesauri]:
            all_by_name = {t.name: t for t in self._thesauri_repo.get(language)}
            all_by_id = {t.id: t for t in self._thesauri_repo.get(language)}
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

        return await asyncio.to_thread(_fetch)

    async def create_thesauri(self, name: str, values: list[str], language: str) -> dict:
        def _call() -> dict:
            payload = [{"label": label} for label in values]
            return self._thesauri_repo.create(name=name, values=payload, language=language)

        return await asyncio.to_thread(_call)

    async def update_thesauri(self, name: str, values: list[str], language: str) -> dict:
        def _call() -> dict:
            existing = self._thesauri_repo.get(language)
            target = next((t for t in existing if t.name == name), None)
            if target is None:
                raise ValueError(f"Thesauri '{name}' not found")
            existing_map = {v.label: v.id for v in target.values}
            for label in values:
                existing_map.setdefault(label, label)
            return self._thesauri_repo.add_value(
                thesauri_id=target.id,
                thesauri_name=target.name,
                thesauri_values=existing_map,
                language=language,
            )

        return await asyncio.to_thread(_call)

    async def delete_thesauri(self, name: str, language: str) -> dict:
        def _call() -> dict:
            existing = self._thesauri_repo.get(language)
            target = next((t for t in existing if t.name == name), None)
            if target is None:
                raise ValueError(f"Thesauri '{name}' not found")
            return self._thesauri_repo.delete_unassigned(thesauri_id=target.id, language=language)

        return await asyncio.to_thread(_call)

    async def get_templates(self) -> list[AgentTemplate]:
        def _fetch() -> list[AgentTemplate]:
            return [self._template_mapper.to_agent(t) for t in self._template_repo.get()]

        return await asyncio.to_thread(_fetch)

    async def get_templates_by_names(self, names: list[str]) -> list[AgentTemplate]:
        def _fetch() -> list[AgentTemplate]:
            all_by_name = {t.name: t for t in self._template_repo.get()}
            all_by_id = {t.id: t for t in self._template_repo.get()}
            result: list[AgentTemplate] = []
            for name in names:
                found = all_by_name.get(name) or all_by_id.get(name)
                if found is None:
                    continue
                result.append(self._template_mapper.to_agent(found))
            return result

        return await asyncio.to_thread(_fetch)

    async def get_template_names(self) -> list[str]:
        def _fetch() -> list[str]:
            return [t.name for t in self._template_repo.get()]

        return await asyncio.to_thread(_fetch)

    async def create_template(self, template: AgentTemplate, language: str) -> dict:
        def _call() -> dict:
            api_template = self._template_mapper.to_api(template)
            return self._template_repo.set(language=language, template=api_template)

        return await asyncio.to_thread(_call)

    async def update_template(self, template: AgentTemplate, language: str) -> dict:
        def _call() -> dict:
            existing = self._template_repo.get_by_name(template.name)
            if existing is None:
                raise ValueError(f"Template '{template.name}' not found")
            api_template = self._template_mapper.to_api(template)
            api_template.id = existing.id
            return self._template_repo.set(language=language, template=api_template)

        return await asyncio.to_thread(_call)

    async def delete_template(self, name: str, language: str) -> dict:
        def _call() -> dict:
            target = self._template_repo.get_by_name(name)
            if target is None:
                raise ValueError(f"Template '{name}' not found")
            return self._template_repo.delete_empty_template(template_id=target.id)

        return await asyncio.to_thread(_call)
