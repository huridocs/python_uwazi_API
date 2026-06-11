from loguru import logger
from pydantic_ai import RunContext

from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies
from uwazi_api.domain.exceptions import DomainError


async def get_stats(
    ctx: RunContext[UwaziAgentToolsDependencies],
    language: str = "en",
) -> dict | str:
    logger.info("get_stats(language={})", language)
    """Fetch aggregate statistics about the Uwazi instance.

    Returns the total entity count, a breakdown of how many entities
    belong to each template, and how many entities use each thesaurus
    value.  Use this to understand data volume and distribution before
    deciding how to approach a task.

    Args:
        language: ISO 639-1 language code (default "en").  The language
            affects thesaurus value labels in the response.

    Returns:
        A dictionary with three keys:
        - ``total_entities`` (int): total number of entities across all
          templates (including unpublished).
        - ``templates`` (list[dict]): each dict has ``template_id``,
          ``template_name``, and ``count`` (entities using that template),
          sorted by count descending.
        - ``thesauri`` (list[dict]): each dict has ``thesaurus_id``,
          ``thesaurus_name``, ``value_id``, ``value_label``, and
          ``count`` (entities with that value), sorted by count descending.
        On error, returns a string describing the problem.
    """
    if ctx.deps.stats_api is None:
        return "Error: stats_api is not configured. Cannot fetch statistics."
    try:
        stats = await ctx.deps.stats_api.get_stats(language=language)
        return stats.model_dump()
    except DomainError as exc:
        logger.error("get_stats FAILED: {}", exc)
        return f"Error fetching statistics: {exc}. Please check the Uwazi connection and retry."
