from pydantic_ai import RunContext

from uwazi_agent.domain.agent_page import AgentPage
from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies


async def get_pages_by_shared_ids(
    ctx: RunContext[UwaziAgentToolsDependencies],
    shared_ids: list[str],
    language: str = "en",
) -> list[AgentPage]:
    """Fetch full page details (body + JavaScript) by their ``shared_id``.

    Use this when you already know the ``shared_id`` of one or more pages
    and want to read their full markdown/HTML ``content`` and ``javascript``
    — for example before editing a page so you can preserve the parts you
    are not changing. If you only have a title, call ``list_pages`` first to
    discover the ``shared_id``; never invent ids.

    Args:
        shared_ids: The Uwazi page shared ids to look up. Unknown ids are
            silently skipped from the result.
        language: ISO 639-1 language code. Defaults to "en".

    Returns:
        The matching pages with their full ``content`` and ``javascript``.
    """
    if ctx.deps.page_api is None:
        raise RuntimeError("Page tools are not configured: `page_api` is missing on dependencies.")
    return await ctx.deps.page_api.get_pages_by_shared_ids(shared_ids=shared_ids, language=language)
