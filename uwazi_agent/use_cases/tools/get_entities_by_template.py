from loguru import logger
from pydantic_ai import RunContext

from uwazi_agent.configuration import ENTITIES_LIMIT_FOR_LLM_MODEL
from uwazi_agent.domain.agent_entity_search_result import AgentEntitySearchResult
from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies
from uwazi_agent.use_cases.tools.fail_forward import suggest_template_names
from uwazi_api.domain.exceptions import DomainError


async def get_entities_by_template(
    ctx: RunContext[UwaziAgentToolsDependencies],
    template_name: str,
    language: str = "en",
    limit: int = 10000,
) -> AgentEntitySearchResult | str:
    logger.info("get_entities_by_template(template_name={!r}, language={!r}, limit={!r})", template_name, language, limit)
    """Fetch all entities belonging to a specific template.

    Use this tool to list every entity of a given template without needing a
    search term. This is the most efficient way to discover all entities of a
    particular type (e.g. all "Judgments" or all "Reports").

    The result is structured the same way as ``search_entities_by_text``: a
    summary (count, breakdown by template, sample titles and shared_ids) plus a
    small number of fully-resolved example entities. Use the example
    ``shared_id`` values with ``get_entities_by_shared_ids`` to fetch the
    exact records you need.

    Cost control rules:
        * ``limit`` caps how many entities are fetched; prefer the default
          and only raise it when you truly need all records.
        * The result is always summarised. The full payload of every entity
          is never returned by this tool.
        * Heavy fields (``documents`` and ``attachments``) are stripped
          from the example entities.

    Entity store:
        * All fetched entities are automatically stored in the session entity
          store for batch processing via the Python agent.
        * When the result count exceeds ``ENTITIES_LIMIT_FOR_LLM_MODEL``,
          suggest using the Python agent for processing instead of handling
          entities individually.

    Args:
        template_name: The template name whose entities you want to list.
            Pass the template *name*, never its id.
        language: ISO 639-1 language code. Defaults to "en".
        limit: Maximum number of entities to fetch. Defaults to 10000.

    Returns:
        A search result with a ``summary`` (count, by-template breakdown,
        sample titles, sample shared_ids) and a few ``examples`` to
        illustrate the shape of the entities. On error, returns a string
        with suggestions to retry with the correct template name.
    """
    if ctx.deps.entity_api is None:
        return "Error: Entity tools are not configured: `entity_api` is missing on dependencies."
    try:
        result = await ctx.deps.entity_api.get_entities_by_template(
            template_name=template_name,
            language=language,
            limit=limit,
        )
    except DomainError:
        return await suggest_template_names(ctx.deps, template_name)
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
