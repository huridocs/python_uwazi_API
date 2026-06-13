import asyncio
import json
from typing import Optional, cast

from loguru import logger
from requests.exceptions import RequestException

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
from uwazi_agent.domain.agent_publish_status import AgentPublishStatus
from uwazi_agent.domain.agent_relationship_type import AgentRelationshipType
from uwazi_agent.domain.agent_search_filter import AgentSearchFilter
from uwazi_agent.domain.agent_template import AgentTemplate
from uwazi_agent.domain.agent_thesauri import AgentThesauri, AgentThesauriGroup
from uwazi_agent.domain.agent_relationship_create import AgentRelationshipCreate
from uwazi_agent.domain.agent_relationship_mutation_result import AgentRelationshipMutationResult
from uwazi_agent.ports.entity_api_port import EntityApiPort
from uwazi_agent.ports.page_api_port import PageApiPort
from uwazi_agent.ports.relationship_api_port import RelationshipApiPort
from uwazi_agent.ports.relationship_type_api_port import RelationshipTypeApiPort
from uwazi_agent.ports.settings_api_port import SettingsApiPort
from uwazi_agent.ports.stats_api_port import StatsApiPort
from uwazi_agent.ports.template_api_port import TemplateApiPort
from uwazi_agent.ports.thesauri_api_port import ThesauriApiPort
from uwazi_api.client import UwaziClient
from uwazi_api.domain.entity import Entity
from uwazi_api.domain.exceptions import (
    EntityNotFoundError,
    PageNotFoundError,
    PropertyNotFilterableError,
    SearchError,
    UploadError,
)
from uwazi_api.domain.stats import SearchStats
from uwazi_api.domain.language import Language
from uwazi_api.domain.menu_link import MenuLink
from uwazi_api.domain.search_filters import DateRange, SearchFilters, SelectFilter


def _categorize_publish_error(exc: Exception) -> str:
    msg = str(exc).lower()
    if "429" in msg or "rate" in msg or "too many" in msg:
        return "RATE_LIMITED"
    if "not found" in msg or "404" in msg:
        return "NOT_FOUND"
    if "permission" in msg or "403" in msg or "401" in msg or "unauthor" in msg:
        return "PERMISSION_DENIED"
    if isinstance(exc, RequestException):
        return "RATE_LIMITED"
    return "INTERNAL"


def _coerce_error_code(code: str):
    from uwazi_agent.domain.agent_entity_mutation_result import MutationErrorCode

    valid: tuple[MutationErrorCode, ...] = (
        "NOT_FOUND",
        "ALREADY_PUBLISHED",
        "NOT_PUBLISHED",
        "PERMISSION_DENIED",
        "RATE_LIMITED",
        "TEMPLATE_MISMATCH",
        "INVALID_LABEL",
        "INTERNAL",
    )
    if code in valid:
        return cast(MutationErrorCode, code)
    return cast(MutationErrorCode, "INTERNAL")


class UwaziApiAdapter(
    ThesauriApiPort,
    TemplateApiPort,
    EntityApiPort,
    PageApiPort,
    RelationshipApiPort,
    RelationshipTypeApiPort,
    SettingsApiPort,
    StatsApiPort,
):
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
        self._relationship_repo = self.client.relationships
        self._settings_repo = self.client.settings
        self._menu_links_repo = self.client.menu_links
        self._stats_repo = self.client.stats
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
            return [_to_agent_thesauri(t) for t in self._thesauri_repo.get(language)]

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
                result.append(_to_agent_thesauri(found))
            return result

        return await asyncio.to_thread(_fetch)

    async def create_thesauri(
        self,
        name: str,
        values: list[str],
        language: str,
        groups: list[AgentThesauriGroup] | None = None,
    ) -> dict:
        def _call() -> dict:
            payload = _build_thesauri_values(values, groups)
            return self._thesauri_repo.create(name=name, values=payload, language=language)

        return await asyncio.to_thread(_call)

    async def update_thesauri(
        self,
        name: str,
        values: list[str],
        language: str,
        groups: list[AgentThesauriGroup] | None = None,
    ) -> dict:
        def _call() -> dict:
            existing = self._thesauri_repo.get(language)
            target = next((t for t in existing if t.name == name), None)
            if target is None:
                raise ValueError(f"Thesauri '{name}' not found")
            merged_values = _merge_thesauri_values(target, values, groups)
            return self._thesauri_repo.update(
                thesauri_id=target.id,
                name=target.name,
                values=merged_values,
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
            api_template = self._template_mapper.to_api(template, existing=existing)
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

    async def get_entities_by_shared_ids(
        self, shared_ids: list[str], language: str, limit: int = 10000
    ) -> list[AgentEntity]:
        def _fetch() -> list[AgentEntity]:
            result: list[AgentEntity] = []
            for shared_id in shared_ids[:limit]:
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

    async def get_entities_by_template(
        self,
        template_name: str,
        language: str,
        limit: int,
    ) -> AgentEntitySearchResult:
        def _fetch() -> AgentEntitySearchResult:
            entities = self._search_repo.get(
                start_from=0,
                batch_size=limit,
                template_name=template_name,
                language=language,
            )
            return self._summarize(entities, limit, language)

        return await asyncio.to_thread(_fetch)

    async def search_entities_by_filter(
        self,
        template_name: str,
        filters: list[AgentSearchFilter],
        language: str,
        limit: int,
        published: bool | None = None,
    ) -> AgentEntitySearchResult:
        def _search() -> AgentEntitySearchResult:
            self._validate_filter_properties(template_name, filters)
            search_filters = SearchFilters()
            for f in filters:
                if f.values is not None:
                    search_filters.add(f.property_name, SelectFilter(values=list(f.values)))
                elif f.date_from is not None or f.date_to is not None:
                    search_filters.add(
                        f.property_name,
                        DateRange(from_=f.date_from, to=f.date_to),
                    )
                else:
                    raise SearchError(
                        f"Filter on '{f.property_name}' must set either 'values' (select) or 'date_from'/'date_to' (date)."
                    )
            entities = self._search_repo.search_by_filter(
                filters=search_filters,
                template_name=template_name,
                start_from=0,
                batch_size=limit,
                language=language,
                published=published,
            )
            return self._summarize(entities, limit, language)

        return await asyncio.to_thread(_search)

    def _validate_filter_properties(
        self,
        template_name: str,
        filters: list[AgentSearchFilter],
    ) -> None:
        """Raise ``PropertyNotFilterableError`` if any filter targets a property
        that is not flagged ``useAsFilter`` on the template.

        Runs as a synchronous helper inside the adapter's own worker thread
        (the whole ``search_entities_by_filter`` body is wrapped in
        ``asyncio.to_thread``) so the template lookup hits the cached
        ``template_repo.get()`` instead of a fresh HTTP request.
        """
        if not filters:
            return
        template = self._template_repo.get_by_name(template_name)
        if template is None:
            return
        all_props = list(template.properties) + list(template.common_properties)
        filterable = {p.name for p in all_props if p.filter}
        prop_by_name = {p.name: p for p in all_props}
        for f in filters:
            prop = prop_by_name.get(f.property_name)
            if prop is not None and not prop.filter:
                raise PropertyNotFilterableError(
                    property_name=f.property_name,
                    template_name=template_name,
                    filterable_properties=sorted(filterable),
                )

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

    async def set_entities_publish_status(self, shared_ids: list[str], published: bool) -> list[AgentEntityMutationResult]:
        def _call_once() -> dict[str, Optional[str]]:
            if published:
                return self._entity_repo.publish_entities(list(shared_ids))
            return self._entity_repo.unpublish_entities(list(shared_ids))

        def _call() -> list[AgentEntityMutationResult]:
            try:
                per_id = _call_once()
            except (UploadError, ValueError) as exc:
                logger.error("set_entities_publish_status FAILED: {}", exc)
                return [
                    AgentEntityMutationResult(
                        shared_id=sid,
                        success=False,
                        error=str(exc),
                        error_code=_categorize_publish_error(exc),
                    )
                    for sid in shared_ids
                ]
            except RequestException as exc:
                logger.error("set_entities_publish_status NETWORK FAILED: {}", exc)
                return [
                    AgentEntityMutationResult(
                        shared_id=sid,
                        success=False,
                        error=str(exc),
                        error_code="RATE_LIMITED",
                    )
                    for sid in shared_ids
                ]

            results: list[AgentEntityMutationResult] = []
            for sid in shared_ids:
                err = per_id.get(sid)
                if err is None:
                    results.append(AgentEntityMutationResult(shared_id=sid, success=True))
                else:
                    results.append(
                        AgentEntityMutationResult(
                            shared_id=sid,
                            success=False,
                            error=err,
                            error_code=_categorize_publish_error(Exception(err)),
                        )
                    )
            return results

        return await asyncio.to_thread(_call)

    async def get_publish_status(self, shared_ids: list[str], language: str) -> list[AgentPublishStatus]:
        def _call() -> list[AgentPublishStatus]:
            results: list[AgentPublishStatus] = []
            for shared_id in shared_ids:
                try:
                    permissions = self._entity_repo._get_entity_permissions(shared_id)
                except Exception:
                    permissions = []
                is_public = any(p.get("refId") == "public" and p.get("type") == "public" for p in permissions)
                results.append(
                    AgentPublishStatus(
                        shared_id=shared_id,
                        published=is_public,
                        permissions=permissions,
                    )
                )
            return results

        return await asyncio.to_thread(_call)

    # --- RelationshipTypeApiPort -----------------------------------------

    async def get_relationship_types(self) -> list[AgentRelationshipType]:
        def _fetch() -> list[AgentRelationshipType]:
            self._relationship_repo.clear_cache()
            return [AgentRelationshipType(name=rt.name) for rt in self._relationship_repo.get_relation_types()]

        return await asyncio.to_thread(_fetch)

    async def get_relationship_type_names(self) -> list[str]:
        def _fetch() -> list[str]:
            self._relationship_repo.clear_cache()
            return [rt.name for rt in self._relationship_repo.get_relation_types()]

        return await asyncio.to_thread(_fetch)

    async def create_relationship_type(self, name: str, language: str) -> dict:
        def _call() -> dict:
            existing = self._relationship_repo.get_relation_type_by_name(name)
            if existing is not None:
                raise ValueError(f"Relationship type '{name}' already exists")
            return self._relationship_repo.create_relation_type(name=name, language=language)

        return await asyncio.to_thread(_call)

    async def update_relationship_type(self, name: str, new_name: str, language: str) -> dict:
        def _call() -> dict:
            target = self._relationship_repo.get_relation_type_by_name(name)
            if target is None:
                raise ValueError(f"Relationship type '{name}' not found")
            return self._relationship_repo.update_relation_type(relation_type_id=target.id, name=new_name, language=language)

        return await asyncio.to_thread(_call)

    async def delete_relationship_type(self, name: str, language: str) -> dict:
        def _call() -> dict:
            target = self._relationship_repo.get_relation_type_by_name(name)
            if target is None:
                raise ValueError(f"Relationship type '{name}' not found")
            return self._relationship_repo.delete_relation_type(relation_type_id=target.id, language=language)

        return await asyncio.to_thread(_call)

    # --- RelationshipApiPort --------------------------------------------

    async def create_relationships(
        self,
        relationships: list[AgentRelationshipCreate],
        language: str = "en",
    ) -> list[AgentRelationshipMutationResult]:
        def _call() -> list[AgentRelationshipMutationResult]:
            results: list[AgentRelationshipMutationResult] = []
            for rel in relationships:
                try:
                    rel_type_id = self._relationship_repo.resolve_relation_type_id(rel.relationship_type_name)
                    if rel_type_id is None:
                        results.append(
                            AgentRelationshipMutationResult(
                                success=False,
                                error=f"Relationship type '{rel.relationship_type_name}' not found",
                                from_entity_shared_id=rel.from_entity_shared_id,
                                to_entity_shared_id=rel.to_entity_shared_id,
                                relationship_type_name=rel.relationship_type_name,
                            )
                        )
                        continue

                    rel_from: dict[str, object] = {
                        "entity": rel.from_entity_shared_id,
                        "template": None,
                    }
                    if rel.file_id:
                        rel_from["file"] = rel.file_id
                    if rel.reference_text:
                        rel_from["reference"] = {"text": rel.reference_text, "selectionRectangles": []}

                    rel_to: dict[str, object] = {
                        "entity": rel.to_entity_shared_id,
                        "template": rel_type_id,
                    }

                    json_data = {"delete": [], "save": [[rel_from, rel_to]]}

                    response = self._relationship_repo.http.request_adapter.post(
                        url=f"{self._relationship_repo.http.url}/api/relationships/bulk",
                        headers=self._relationship_repo.http.headers,
                        cookies={"locale": language},
                        data=json.dumps(json_data),
                    )
                    if response.status_code != 200:
                        results.append(
                            AgentRelationshipMutationResult(
                                success=False,
                                error=f"API error ({response.status_code})",
                                from_entity_shared_id=rel.from_entity_shared_id,
                                to_entity_shared_id=rel.to_entity_shared_id,
                                relationship_type_name=rel.relationship_type_name,
                            )
                        )
                    else:
                        results.append(
                            AgentRelationshipMutationResult(
                                success=True,
                                from_entity_shared_id=rel.from_entity_shared_id,
                                to_entity_shared_id=rel.to_entity_shared_id,
                                relationship_type_name=rel.relationship_type_name,
                            )
                        )
                except Exception as exc:
                    results.append(
                        AgentRelationshipMutationResult(
                            success=False,
                            error=str(exc),
                            from_entity_shared_id=rel.from_entity_shared_id,
                            to_entity_shared_id=rel.to_entity_shared_id,
                            relationship_type_name=rel.relationship_type_name,
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
                        css=page.css,
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
                    if update.css is not None:
                        if update.css == "":
                            metadata.pop("css", None)
                        else:
                            metadata["css"] = update.css
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
        all_agent_entities = [self._trim_entity(e, language) for e in entities]

        result = AgentEntitySearchResult(
            summary=AgentEntitySummary(
                count=count,
                by_template=by_template,
                sample_titles=sample_titles[:example_count],
                shared_ids=shared_ids[:example_count],
            ),
            examples=examples,
        )
        result._all_entities = all_agent_entities
        return result

    async def get_languages(self) -> list[Language]:
        def _fetch() -> list[Language]:
            return self._settings_repo.get_languages()

        return await asyncio.to_thread(_fetch)

    async def get_menu_links(self) -> list[MenuLink]:
        def _fetch() -> list[MenuLink]:
            return self._menu_links_repo.get_all()

        return await asyncio.to_thread(_fetch)

    async def set_menu_links(self, links: list[MenuLink]) -> list[MenuLink]:
        def _call() -> list[MenuLink]:
            return self._menu_links_repo.replace_all(links)

        return await asyncio.to_thread(_call)

    # --- StatsApiPort -------------------------------------------------------

    async def get_stats(self, language: str = "en") -> SearchStats:
        def _fetch() -> SearchStats:
            return self._stats_repo.get_stats(language=language)

        return await asyncio.to_thread(_fetch)


def _to_agent_thesauri(api_thesauri) -> AgentThesauri:
    """Split a Uwazi thesaurus into top-level values and named groups.

    A Uwazi value is a *group* when it carries its own nested ``values``; its
    children become the group's value labels. All other values are top-level.
    """
    top_values: list[str] = []
    groups: list[AgentThesauriGroup] = []
    for value in api_thesauri.values:
        if value.values:
            groups.append(AgentThesauriGroup(name=value.label, values=[child.label for child in value.values]))
        else:
            top_values.append(value.label)
    return AgentThesauri(name=api_thesauri.name, values=top_values, groups=groups)


def _build_thesauri_values(
    values: list[str],
    groups: list[AgentThesauriGroup] | None,
) -> list[dict]:
    """Build the Uwazi value payload from flat values plus named groups."""
    payload: list[dict] = [{"label": label} for label in values]
    for group in groups or []:
        payload.append(
            {
                "label": group.name,
                "values": [{"label": child} for child in group.values],
            }
        )
    return payload


def _merge_thesauri_values(
    existing,
    values: list[str],
    groups: list[AgentThesauriGroup] | None,
) -> list[dict]:
    """Merge new flat values and groups into an existing thesaurus' value tree.

    Existing values and their ids are preserved (Uwazi keeps a value id stable
    so entities that reference it are not broken). New flat labels are appended.
    For each incoming group, an existing group with the same name is extended
    with any new child labels; otherwise the group is created.
    """
    merged: list[dict] = []
    existing_top_labels: set[str] = set()
    existing_groups: dict[str, dict] = {}

    for value in existing.values:
        if value.values:
            group_entry = {
                "label": value.label,
                "id": value.id,
                "values": [{"label": child.label, "id": child.id} for child in value.values],
            }
            merged.append(group_entry)
            existing_groups[value.label] = group_entry
        else:
            merged.append({"label": value.label, "id": value.id})
            existing_top_labels.add(value.label)

    for label in values:
        if label not in existing_top_labels:
            merged.append({"label": label})
            existing_top_labels.add(label)

    for group in groups or []:
        target = existing_groups.get(group.name)
        if target is None:
            target = {"label": group.name, "values": []}
            merged.append(target)
            existing_groups[group.name] = target
        existing_children = {child["label"] for child in target["values"]}
        for child in group.values:
            if child not in existing_children:
                target["values"].append({"label": child})
                existing_children.add(child)

    return merged
