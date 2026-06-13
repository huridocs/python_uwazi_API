"""The single merged entity-read tool used by the entity agent.

The entity agent previously exposed four near-identical read tools
(``search_entities_by_text``, ``search_entities_by_filter``,
``get_entities_by_template``, ``get_entities_by_shared_ids``) which the
LLM had to choose between. They all did the same thing — fetch entities
from Uwazi, normalise the shape, store them in the session entity store,
and auto-hand off to the Python agent when the result is large.

This module collapses them into a single ``query_entities`` tool with a
discriminated ``mode`` parameter and a unified return shape. It also
exploits an in-memory trim cache on :class:`EntityStore` so the common
"search then re-fetch by id" pattern no longer triggers redundant HTTP
calls and remapping.
"""

from __future__ import annotations

from typing import Literal

from loguru import logger
from pydantic_ai import RunContext

from uwazi_agent.configuration import ENTITIES_LIMIT_FOR_LLM_MODEL
from uwazi_agent.domain.agent_entity import AgentEntity
from uwazi_agent.domain.agent_entity_search_result import AgentEntitySearchResult
from uwazi_agent.domain.agent_search_filter import AgentSearchFilter
from uwazi_agent.ports.entity_api_port import EntityApiPort
from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies
from uwazi_agent.use_cases.tools.entity_store import EntityStore
from uwazi_agent.use_cases.tools.fail_forward import suggest_template_names
from uwazi_api.domain.exceptions import DomainError, PropertyNotFilterableError

QueryMode = Literal["by_text", "by_filter", "by_template", "by_ids"]


def _apply_overflow_hint(
    result: AgentEntitySearchResult, deps: UwaziAgentToolsDependencies
) -> AgentEntitySearchResult:
    """Set the standard "delegate to Python agent" hint when the count
    exceeds the per-LLM-call cap, and remember it on the store."""
    if result.summary.count > ENTITIES_LIMIT_FOR_LLM_MODEL:
        deps.entity_store.needs_python_agent = True
        result.summary.note = (
            f"{result.summary.count} entities found and stored in the entity store. "
            f"This exceeds the LLM limit of {ENTITIES_LIMIT_FOR_LLM_MODEL}. "
            f"You MUST delegate to the Python agent for batch processing."
        )
    return result


async def query_entities(
    ctx: RunContext[UwaziAgentToolsDependencies],
    mode: QueryMode,
    language: str = "en",
    limit: int = 10000,
    search_term: str | None = None,
    template_name: str | None = None,
    filters: list[AgentSearchFilter] | None = None,
    published: bool | None = None,
    shared_ids: list[str] | None = None,
) -> AgentEntitySearchResult | list[AgentEntity] | str:
    """Fetch entities from Uwazi through a single unified entrypoint.

    The ``mode`` argument picks the discovery strategy:

    * ``"by_text"`` — fuzzy free-text search. Set ``search_term`` and
      optionally ``template_name``.
    * ``"by_filter"`` — structured exact-match queries on a template's
      ``use_as_filter`` properties. Set ``template_name`` and ``filters``.
    * ``"by_template"`` — list every entity of one template. Set
      ``template_name``.
    * ``"by_ids"`` — fetch full details for known ``shared_id`` values.
      Set ``shared_ids``. This mode is the only one that returns a
      ``list[AgentEntity]`` instead of a search result. It checks the
      session entity cache first, so entities already seen via a search
      or previous ``by_ids`` call are returned with **no HTTP traffic**.

    Cost control:
        * ``limit`` caps how many entities the underlying call inspects;
          the default is 10 000 (effectively "everything" for any
          reasonable instance).
        * All search modes store the full result in the session entity
          store. When the total exceeds ``ENTITIES_LIMIT_FOR_LLM_MODEL``,
          the result is annotated with a hard handoff message telling
          you to delegate to the Python agent.
        * ``"by_ids"`` is the right tool when you already know the
          ``shared_id`` of the entities you want — it does not pay the
          cost of re-mapping entities that are already cached from a
          previous search.

    Args:
        mode: One of ``"by_text"``, ``"by_filter"``, ``"by_template"``,
            ``"by_ids"``. Selects which other arguments are required.
        language: ISO 639-1 language code for the read. Defaults to "en".
        limit: Maximum number of entities to fetch from Uwazi. Defaults
            to 10 000.
        search_term: Free-text query, used by ``"by_text"``.
        template_name: Restrict to a single template (by name, not id).
            Required by ``"by_filter"`` and ``"by_template"``; optional
            for ``"by_text"``.
        filters: List of filter conditions combined with AND, used by
            ``"by_filter"``. Each filter must target a property marked
            ``use_as_filter`` on the template.
        published: Optional publish-state filter (``"by_filter"`` only).
        shared_ids: List of ``shared_id`` values to fetch, used by
            ``"by_ids"``.

    Returns:
        For ``"by_text" | "by_filter" | "by_template"``: an
        :class:`AgentEntitySearchResult` with a ``summary`` (count, by
        template, sample titles and shared_ids) and a few ``examples``.
        For ``"by_ids"``: a list of :class:`AgentEntity`. On error,
        returns a string describing the problem.
    """
    api = ctx.deps.entity_api
    if api is None:
        return "Error: Entity tools are not configured: `entity_api` is missing on dependencies."
    store = ctx.deps.entity_store

    if mode == "by_text":
        return await _by_text(api, store, ctx.deps, search_term, template_name, language, limit)
    if mode == "by_filter":
        return await _by_filter(api, store, ctx.deps, template_name, filters or [], language, limit, published)
    if mode == "by_template":
        return await _by_template(api, store, ctx.deps, template_name, language, limit)
    if mode == "by_ids":
        return await _by_ids(api, store, shared_ids, language, limit)
    return (
        f"Error: unknown mode '{mode}'. "
        "Use one of: 'by_text', 'by_filter', 'by_template', 'by_ids'."
    )


async def _by_text(
    api: EntityApiPort,
    store: EntityStore,
    deps: UwaziAgentToolsDependencies,
    search_term: str | None,
    template_name: str | None,
    language: str,
    limit: int,
):
    if not search_term:
        return "Error: 'by_text' mode requires `search_term`."
    try:
        result = await api.search_entities_by_text(
            search_term=search_term,
            template_name=template_name,
            language=language,
            limit=limit,
        )
    except DomainError as exc:
        logger.error("query_entities(by_text) FAILED: {} | error={}", search_term, exc)
        if template_name and "template" in str(exc).lower():
            return await suggest_template_names(deps, template_name)
        return f"Error searching entities: {exc}. Please check your search parameters and retry."
    if result._all_entities:
        store.add_entities(result._all_entities)
    return _apply_overflow_hint(result, deps)


async def _by_filter(
    api: EntityApiPort,
    store: EntityStore,
    deps: UwaziAgentToolsDependencies,
    template_name: str | None,
    filters: list[AgentSearchFilter],
    language: str,
    limit: int,
    published: bool | None,
):
    if not template_name:
        return "Error: 'by_filter' mode requires `template_name`."
    try:
        result = await api.search_entities_by_filter(
            template_name=template_name,
            filters=filters,
            language=language,
            limit=limit,
            published=published,
        )
    except PropertyNotFilterableError as exc:
        logger.error(
            "query_entities(by_filter) REJECTED: template={} property={} filterable={}",
            template_name,
            exc.property_name,
            exc.filterable_properties,
        )
        return (
            f"Error: property '{exc.property_name}' is not filterable on template "
            f"'{exc.template_name}'. Only properties with `use_as_filter` set on the template "
            f"can be passed to search_entities_by_filter. Filterable properties on this "
            f"template: {exc.filterable_properties}. Call get_templates_by_names to inspect "
            f"the template's properties and their `use_as_filter` flag, then retry with a "
            f"filterable property."
        )
    except DomainError as exc:
        logger.error(
            "query_entities(by_filter) FAILED: template={} filters={} error={}",
            template_name,
            filters,
            exc,
        )
        if "not found" in str(exc).lower() and "template" in str(exc).lower():
            return await suggest_template_names(deps, template_name)
        return (
            f"Error filtering entities: {exc}. Confirm the property is marked use_as_filter "
            "on the template and that select values are valid thesaurus labels, then retry."
        )
    if result._all_entities:
        store.add_entities(result._all_entities)
    return _apply_overflow_hint(result, deps)


async def _by_template(
    api: EntityApiPort,
    store: EntityStore,
    deps: UwaziAgentToolsDependencies,
    template_name: str | None,
    language: str,
    limit: int,
):
    if not template_name:
        return "Error: 'by_template' mode requires `template_name`."
    try:
        result = await api.get_entities_by_template(
            template_name=template_name, language=language, limit=limit
        )
    except DomainError as exc:
        logger.error(
            "query_entities(by_template) FAILED: template={} error={}",
            template_name,
            exc,
        )
        return await suggest_template_names(deps, template_name)
    if result._all_entities:
        store.add_entities(result._all_entities)
    return _apply_overflow_hint(result, deps)


async def _by_ids(
    api: EntityApiPort,
    store: EntityStore,
    shared_ids: list[str] | None,
    language: str,
    limit: int,
) -> list[AgentEntity] | str:
    if not shared_ids:
        return "Error: 'by_ids' mode requires `shared_ids` (a non-empty list)."

    # 1. Serve as many ids as possible from the session entity cache —
    #    no HTTP call, no remapping.
    cached = store.cache_get_many(shared_ids, language=language)
    cache_index = {(e.shared_id, e.language or "en"): e for e in cached}
    missing = store.cache_misses(shared_ids, language=language)

    fetched: list[AgentEntity] = []
    if missing:
        try:
            fetched = await api.get_entities_by_shared_ids(
                shared_ids=missing, language=language, limit=limit
            )
        except DomainError as exc:
            logger.error(
                "query_entities(by_ids) FAILED: shared_ids={} error={}",
                shared_ids,
                exc,
            )
            return f"Error fetching entities by shared_ids: {exc}. Please verify the shared_ids and retry."

    # Order the response to match the request order, deduplicating.
    result: list[AgentEntity] = []
    seen: set[str] = set()
    for sid in shared_ids:
        ent = cache_index.get((sid, language))
        if ent is None:
            for f in fetched:
                if f.shared_id == sid and f.shared_id not in seen:
                    ent = f
                    break
        if ent is not None and ent.shared_id not in seen:
            result.append(ent)
            seen.add(ent.shared_id)

    if fetched:
        store.add_entities(fetched)

    if len(result) > ENTITIES_LIMIT_FOR_LLM_MODEL:
        store.needs_python_agent = True
        return (
            f"{len(result)} entities fetched and stored in the entity store. "
            f"This exceeds the LLM limit of {ENTITIES_LIMIT_FOR_LLM_MODEL}. "
            f"You MUST delegate to the Python agent for batch processing."
        )

    if result:
        logger.info(
            "query_entities(by_ids): served {} from cache, {} from Uwazi (total requested {})",
            len(cached),
            len(fetched),
            len(shared_ids),
        )
    return result

