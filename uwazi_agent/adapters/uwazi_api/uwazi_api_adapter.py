import asyncio
from typing import Optional

from uwazi_agent.adapters.template_mapper import TemplateMapperAdapter
from uwazi_agent.adapters.uwazi_api.entity_mapper import EntityMapper
from uwazi_agent.adapters.uwazi_api.page_mapper import PageMapper
from uwazi_agent.adapters.uwazi_api.thesaurus_gateway import build_template_mapper_from_client
from uwazi_agent.domain.agent_entity import AgentEntity
from uwazi_agent.domain.agent_entity_create import AgentEntityCreate
from uwazi_agent.domain.agent_entity_mutation_result import AgentEntityMutationResult
from uwazi_agent.domain.agent_entity_search_result import AgentEntitySearchResult
from uwazi_agent.domain.agent_entity_summary import AgentEntitySummary
from uwazi_agent.domain.agent_page import AgentPage
from uwazi_agent.domain.agent_page_create import AgentPageCreate
from uwazi_agent.domain.agent_page_mutation_result import AgentPageMutationResult
from uwazi_agent.domain.agent_page_summary import AgentPageSummary
from uwazi_agent.domain.agent_page_update import AgentPageUpdate
from uwazi_agent.domain.agent_template import AgentTemplate
from uwazi_agent.domain.agent_thesauri import AgentThesauri
from uwazi_agent.ports.entity_api_port import EntityApiPort
from uwazi_agent.ports.page_api_port import PageApiPort
from uwazi_agent.ports.template_api_port import TemplateApiPort
from uwazi_agent.ports.thesauri_api_port import ThesauriApiPort
from uwazi_api.client import UwaziClient
from uwazi_api.domain.entity import Entity
from uwazi_api.domain.exceptions import EntityNotFoundError, PageNotFoundError, SearchError, UploadError


class UwaziApiAdapter(ThesauriApiPort, TemplateApiPort, EntityApiPort, PageApiPort):
    def __init__(
        self,
        user: Optional[str] = None,
        password: Optional[str] = None,
        url: Optional[str] = None,
        template_mapper: Optional[TemplateMapperAdapter] = None,
        entity_mapper: Optional[EntityMapper] = None,
        page_mapper: Optional[PageMapper] = None,
    ):
        self.client = UwaziClient(user=user, password=password, url=url)
        self._thesauri_repo = self.client.thesauris
        self._template_repo = self.client.templates
        self._entity_repo = self.client.entities
        self._search_repo = self.client.search
        self._pages_repo = self.client.pages
        self._template_mapper = template_mapper or build_template_mapper_from_client(self.client)
        self._entity_mapper = entity_mapper or EntityMapper(
            template_repo=self._template_repo, thesauri_repo=self._thesauri_repo
        )
        self._page_mapper = page_mapper or PageMapper(base_url=self.client.http.url)

    @property
    def entity_mapper(self) -> EntityMapper:
        return self._entity_mapper

    @property
    def page_mapper(self) -> PageMapper:
        return self._page_mapper

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

    # --- EntityApiPort ----------------------------------------------------

    async def create_entities(self, entities: list[AgentEntityCreate], language: str) -> list[AgentEntityMutationResult]:
        def _call() -> list[AgentEntityMutationResult]:
            results: list[AgentEntityMutationResult] = []
            for agent_entity in entities:
                try:
                    api_entity = self._entity_mapper.to_api_for_create(agent_entity, language=language)
                    new_shared_id = self._entity_repo.upload(api_entity, language)
                    results.append(AgentEntityMutationResult(shared_id=new_shared_id, success=True))
                except (UploadError, ValueError, SearchError) as exc:
                    results.append(
                        AgentEntityMutationResult(
                            shared_id="",
                            success=False,
                            error=str(exc),
                        )
                    )
            return results

        return await asyncio.to_thread(_call)

    async def get_entities_by_shared_ids(self, shared_ids: list[str], language: str) -> list[AgentEntity]:
        def _fetch() -> list[AgentEntity]:
            result: list[AgentEntity] = []
            for shared_id in shared_ids:
                try:
                    api_entity = self._entity_repo.get_one(shared_id, language)
                except EntityNotFoundError:
                    continue
                result.append(self._trim_entity(api_entity, language))
            return result

        return await asyncio.to_thread(_fetch)

    async def search_entities_by_text(
        self,
        search_term: str,
        template_name: Optional[str],
        language: str,
        limit: int,
    ) -> AgentEntitySearchResult:
        def _search() -> AgentEntitySearchResult:
            entities = self._search_repo.search_by_text(
                search_term=search_term,
                template_name=template_name,
                start_from=0,
                batch_size=limit,
                language=language,
            )
            return self._summarize(entities, limit, language)

        return await asyncio.to_thread(_search)

    async def update_entities(self, updates: list[AgentEntity], language: str) -> list[AgentEntityMutationResult]:
        def _call() -> list[AgentEntityMutationResult]:
            results: list[AgentEntityMutationResult] = []
            for agent_entity in updates:
                try:
                    api_entity = self._entity_mapper.to_api(agent_entity, language=language)
                    self._entity_repo.update_partially(api_entity, language)
                    results.append(AgentEntityMutationResult(shared_id=agent_entity.shared_id, success=True))
                except (UploadError, ValueError, SearchError) as exc:
                    results.append(
                        AgentEntityMutationResult(
                            shared_id=agent_entity.shared_id,
                            success=False,
                            error=str(exc),
                        )
                    )
            return results

        return await asyncio.to_thread(_call)

    async def delete_entities_by_shared_ids(self, shared_ids: list[str]) -> list[AgentEntityMutationResult]:
        def _call() -> list[AgentEntityMutationResult]:
            results: list[AgentEntityMutationResult] = []
            try:
                self._entity_repo.delete_entities(list(shared_ids))
                for shared_id in shared_ids:
                    results.append(AgentEntityMutationResult(shared_id=shared_id, success=True))
                return results
            except UploadError:
                pass
            for shared_id in shared_ids:
                try:
                    self._entity_repo.delete(shared_id)
                    results.append(AgentEntityMutationResult(shared_id=shared_id, success=True))
                except (EntityNotFoundError, UploadError) as exc:
                    results.append(
                        AgentEntityMutationResult(
                            shared_id=shared_id,
                            success=False,
                            error=str(exc),
                        )
                    )
            return results

        return await asyncio.to_thread(_call)

    # --- PageApiPort ------------------------------------------------------

    async def list_pages(self, language: str) -> list[AgentPageSummary]:
        def _fetch() -> list[AgentPageSummary]:
            return [self._page_mapper.to_summary(p) for p in self._pages_repo.get_all(language)]

        return await asyncio.to_thread(_fetch)

    async def get_pages_by_shared_ids(self, shared_ids: list[str], language: str) -> list[AgentPage]:
        def _fetch() -> list[AgentPage]:
            result: list[AgentPage] = []
            for shared_id in shared_ids:
                try:
                    page = self._pages_repo.get_by_shared_id(shared_id, language)
                except PageNotFoundError:
                    continue
                result.append(self._page_mapper.to_agent(page))
            return result

        return await asyncio.to_thread(_fetch)

    async def create_pages(self, pages: list[AgentPageCreate], language: str) -> list[AgentPageMutationResult]:
        def _call() -> list[AgentPageMutationResult]:
            results: list[AgentPageMutationResult] = []
            for page in pages:
                try:
                    created = self._pages_repo.create(
                        title=page.title,
                        content=page.content,
                        script=page.javascript,
                        entity_view=page.entity_view,
                        language=page.language or language,
                    )
                    results.append(
                        AgentPageMutationResult(
                            shared_id=created.shared_id or "",
                            success=True,
                            url=self._page_mapper.page_url(created),
                        )
                    )
                except (UploadError, ValueError, PageNotFoundError) as exc:
                    results.append(AgentPageMutationResult(shared_id="", success=False, error=str(exc)))
            return results

        return await asyncio.to_thread(_call)

    async def update_pages(self, updates: list[AgentPageUpdate], language: str) -> list[AgentPageMutationResult]:
        def _call() -> list[AgentPageMutationResult]:
            results: list[AgentPageMutationResult] = []
            for update in updates:
                page_language = update.language or language
                try:
                    existing = self._pages_repo.get_by_shared_id(update.shared_id, page_language)
                    if update.title is not None:
                        existing.title = update.title
                    if update.entity_view is not None:
                        existing.entity_view = update.entity_view
                    metadata = dict(existing.metadata or {})
                    if update.content is not None:
                        metadata["content"] = update.content
                    if update.javascript is not None:
                        if update.javascript == "":
                            metadata.pop("script", None)
                        else:
                            metadata["script"] = update.javascript
                    existing.metadata = metadata
                    saved = self._pages_repo.update(existing)
                    results.append(
                        AgentPageMutationResult(
                            shared_id=saved.shared_id or update.shared_id,
                            success=True,
                            url=self._page_mapper.page_url(saved),
                        )
                    )
                except (UploadError, ValueError, PageNotFoundError) as exc:
                    results.append(AgentPageMutationResult(shared_id=update.shared_id, success=False, error=str(exc)))
            return results

        return await asyncio.to_thread(_call)

    async def delete_pages_by_shared_ids(self, shared_ids: list[str], language: str) -> list[AgentPageMutationResult]:
        def _call() -> list[AgentPageMutationResult]:
            results: list[AgentPageMutationResult] = []
            for shared_id in shared_ids:
                try:
                    self._pages_repo.delete(shared_id, language)
                    results.append(AgentPageMutationResult(shared_id=shared_id, success=True))
                except (PageNotFoundError, UploadError) as exc:
                    results.append(AgentPageMutationResult(shared_id=shared_id, success=False, error=str(exc)))
            return results

        return await asyncio.to_thread(_call)

    def _trim_entity(self, api_entity: Entity, language: str) -> AgentEntity:
        api_entity.documents = []
        api_entity.attachments = []
        return self._entity_mapper.to_agent(api_entity, language=language)

    def _summarize(self, entities: list[Entity], limit: int, language: str) -> AgentEntitySearchResult:
        count = len(entities)
        by_template: dict[str, int] = {}
        sample_titles: list[str] = []
        shared_ids: list[str] = []
        for entity in entities:
            template_obj = None
            if entity.template:
                template_obj = self._template_repo.get_by_id(entity.template) or self._template_repo.get_by_name(
                    entity.template
                )
            template_label = template_obj.name if template_obj else (entity.template or "unknown")
            by_template[template_label] = by_template.get(template_label, 0) + 1
            if entity.shared_id:
                shared_ids.append(entity.shared_id)
            if entity.title:
                sample_titles.append(entity.title)

        example_count = min(3, count, limit)
        examples = [self._trim_entity(e, language) for e in entities[:example_count]]

        return AgentEntitySearchResult(
            summary=AgentEntitySummary(
                count=count,
                by_template=by_template,
                sample_titles=sample_titles[:example_count],
                shared_ids=shared_ids[:example_count],
            ),
            examples=examples,
        )
