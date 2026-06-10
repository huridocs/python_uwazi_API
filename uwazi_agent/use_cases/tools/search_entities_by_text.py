from loguru import logger
from pydantic_ai import RunContext

from uwazi_agent.configuration import ENTITIES_LIMIT_FOR_LLM_MODEL
from uwazi_agent.domain.agent_entity_search_result import AgentEntitySearchResult
from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies
from uwazi_agent.use_cases.tools.fail_forward import suggest_template_names
from uwazi_api.domain.exceptions import DomainError


async def search_entities_by_text(
    ctx: RunContext[UwaziAgentToolsDependencies],
    search_term: str,
    template_name: str | None = None,
    language: str = "en",
    limit: int = 10000,
) -> AgentEntitySearchResult | str:
    logger.info(
        "search_entities_by_text(search_term={!r}, template_name={!r}, language={!r}, limit={!r})",
        search_term,
        template_name,
        language,
        limit,
    )
    """Search Uwazi entities by free-text matching their content.

    This is the only way to discover a ``shared_id`` from a title or any
    free-form description the user provides. The result is intentionally
    lightweight: instead of returning every hit in full, you receive a
    summary (count, breakdown by template, a few sample titles) plus a
    small number of fully-resolved example entities. Use the example
    ``shared_id`` values with ``get_entities_by_shared_ids`` to fetch the
    exact records you need.

    Cost control rules:
        * ``limit`` caps how many hits are inspected; prefer the default
          (30) and only raise it for very specific queries.
        * The result is always summarised. The full payload of every entity
          is never returned by this tool.
        * Heavy fields (``documents`` and ``attachments``) are stripped
          from the example entities.

    Entity store:
        * All matched entities are automatically stored in the session entity
          store for batch processing via the Python agent.
        * When the result count exceeds ``ENTITIES_LIMIT_FOR_LLM_MODEL``,
          suggest using the Python agent for processing instead of handling
          entities individually.

    Args:
        search_term: Free-text query, matched against entity content.
        template_name: Optional template name to restrict the search to
            a single template. Pass the template *name*, never its id.
        language: ISO 639-1 language code. Defaults to "en".
        limit: Maximum number of hits to inspect; the summary reflects the
            total matched count, not the limit. Defaults to 30.

    Returns:
        A search result with a ``summary`` (count, by-template breakdown,
        sample titles, sample shared_ids) and a few ``examples`` to
        illustrate the shape of matching entities. On error, returns a
        string with suggestions to retry with corrected parameters.
    """
    if ctx.deps.entity_api is None:
        return "Error: Entity tools are not configured: `entity_api` is missing on dependencies."
    try:
        result = await ctx.deps.entity_api.search_entities_by_text(
            search_term=search_term,
            template_name=template_name,
            language=language,
            limit=limit,
        )
    except DomainError as exc:
        logger.error("search_entities_by_text FAILED: search_term={} template={} error={}", search_term, template_name, exc)
        if template_name and "template" in str(exc).lower():
            return await suggest_template_names(ctx.deps, template_name)
        return f"Error searching entities: {exc}. Please check your search parameters and retry."
    all_entities = result._all_entities
    if all_entities:
        ctx.deps.entity_store.add_entities(all_entities)
    if result.summary.count > ENTITIES_LIMIT_FOR_LLM_MODEL:
        ctx.deps.entity_store.needs_python_agent = True
        result.summary.note = (
            f"{result.summary.count} entities found and stored in the entity store. "
            f"This exceeds the LLM limit of {ENTITIES_LIMIT_FOR_LLM_MODEL}. "
            f"You MUST delegate to the Python agent for batch processing."
        )
    return result
