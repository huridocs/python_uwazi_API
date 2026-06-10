from loguru import logger
from pydantic_ai import RunContext

from uwazi_agent.domain.agent_page_mutation_result import AgentPageMutationResult
from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies
from uwazi_api.domain.exceptions import DomainError


async def delete_pages_by_shared_ids(
    ctx: RunContext[UwaziAgentToolsDependencies],
    shared_ids: list[str],
    language: str = "en",
) -> list[AgentPageMutationResult] | str:
    logger.info("delete_pages_by_shared_ids(shared_ids={!r}, language={!r})", shared_ids, language)
    """Delete one or more pages by their ``shared_id``.

    Deletions are **irreversible**. Always confirm with the user before
    calling this tool. Identify pages by ``shared_id`` only — titles are not
    safe. If you only know a page by name, call ``list_pages`` first to
    discover the ``shared_id`` and surface it to the user for confirmation.

    Partial failures are reported per id; one bad id does not abort the rest.

    Args:
        shared_ids: The Uwazi page shared ids to delete.
        language: ISO 639-1 language code. Defaults to "en".

    Returns:
        A per-page result indicating success or a descriptive ``error``
        (e.g. unknown shared_id). On catastrophic error, returns a string
        describing the problem.
    """
    if ctx.deps.page_api is None:
        return "Error: Page tools are not configured: `page_api` is missing on dependencies."
    try:
        return await ctx.deps.page_api.delete_pages_by_shared_ids(shared_ids=shared_ids, language=language)
    except DomainError as exc:
        return f"Error deleting pages: {exc}. Please verify the shared_ids and retry."
