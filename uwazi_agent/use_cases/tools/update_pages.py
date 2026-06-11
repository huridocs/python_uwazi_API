from loguru import logger
from pydantic_ai import RunContext

from uwazi_agent.domain.agent_page_mutation_result import AgentPageMutationResult
from uwazi_agent.domain.agent_page_update import AgentPageUpdate
from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies
from uwazi_api.domain.exceptions import DomainError


async def update_pages(
    ctx: RunContext[UwaziAgentToolsDependencies],
    updates: list[AgentPageUpdate],
    language: str = "en",
) -> list[AgentPageMutationResult] | str:
    """Apply partial updates to one or more existing pages.

    This is a *partial merge*: only the fields you set are changed; any field
    you leave unset (``None``) is preserved as-is on Uwazi's side. Identify
    each page by its ``shared_id`` — never by title. If you only have a
    title, call ``list_pages`` first; if you intend to tweak (rather than
    fully replace) the body, fetch the current ``content`` with
    ``get_pages_by_shared_ids`` so you do not accidentally drop sections.

    Field semantics:
        * ``title`` — rename the page.
        * ``content`` — replace the markdown/HTML body in full.
        * ``javascript`` — set the page's JavaScript. Pass an empty string
          ("") to remove it; leave ``None`` to keep it unchanged.
        * ``entity_view`` — toggle whether the page is an entity-view template.

    Args:
        updates: The list of partial page updates to apply.
        language: ISO 639-1 language code applied when an update does not set
            its own ``language``. Defaults to "en".

    Returns:
        A per-page result indicating success (with the public ``url``) or a
        descriptive ``error`` (e.g. unknown shared_id). One failure does not
        abort the rest. On catastrophic error, returns a string describing
        the problem.
    """
    if ctx.deps.page_api is None:
        return "Error: Page tools are not configured: `page_api` is missing on dependencies."
    try:
        return await ctx.deps.page_api.update_pages(updates=updates, language=language)
    except DomainError as exc:
        logger.error("update_pages FAILED: {}", exc)
        return f"Error updating pages: {exc}. Please verify the shared_ids and retry."
