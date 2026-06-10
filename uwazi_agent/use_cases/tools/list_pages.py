from pydantic_ai import RunContext

from uwazi_agent.domain.agent_page_summary import AgentPageSummary
from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies


async def list_pages(
    ctx: RunContext[UwaziAgentToolsDependencies],
    language: str = "en",
) -> list[AgentPageSummary]:
    """List the Settings → Pages of a Uwazi instance as compact summaries.

    Use this to discover which pages exist and to find a page's
    ``shared_id`` before fetching, updating, or deleting it. The summaries
    intentionally omit the full body to stay token-cheap; once you know the
    ``shared_id`` you want, call ``get_pages_by_shared_ids`` to read the
    full markdown/HTML and JavaScript.

    Args:
        language: ISO 639-1 language code. Defaults to "en".

    Returns:
        One summary per page: ``shared_id``, ``title``, ``language``,
        public ``url``, and the ``has_markdown`` / ``has_javascript`` flags.
    """
    if ctx.deps.page_api is None:
        raise RuntimeError("Page tools are not configured: `page_api` is missing on dependencies.")
    return await ctx.deps.page_api.list_pages(language=language)
