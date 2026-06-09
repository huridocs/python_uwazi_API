from pydantic_ai import RunContext

from uwazi_agent.domain.agent_entity import AgentEntitySearchResult
from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies


async def search_entities_by_text(
    ctx: RunContext[UwaziAgentToolsDependencies],
    search_term: str,
    template_name: str | None = None,
    language: str = "en",
    limit: int = 30,
) -> AgentEntitySearchResult:
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
        illustrate the shape of matching entities.
    """
    if ctx.deps.entity_api is None:
        raise RuntimeError("Entity tools are not configured: `entity_api` is missing on dependencies.")
    return await ctx.deps.entity_api.search_entities_by_text(
        search_term=search_term,
        template_name=template_name,
        language=language,
        limit=limit,
    )
