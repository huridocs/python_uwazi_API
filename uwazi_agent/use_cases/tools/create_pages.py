from loguru import logger
from pydantic_ai import RunContext

from uwazi_agent.domain.agent_page_create import AgentPageCreate
from uwazi_agent.domain.agent_page_mutation_result import AgentPageMutationResult
from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies
from uwazi_api.domain.exceptions import DomainError


async def create_pages(
    ctx: RunContext[UwaziAgentToolsDependencies],
    pages: list[AgentPageCreate],
    language: str = "en",
) -> list[AgentPageMutationResult] | str:
    """Create one or more brand-new Settings → Pages in Uwazi.

    Use this to publish rich, standalone pages (landing pages, about pages,
    dashboards, documentation, ...). Each page's ``content`` is its body and
    is rendered as Markdown — and raw HTML is allowed inside it, so you can
    produce genuinely beautiful layouts.

    Authoring tips for beautiful pages:
        * Structure with Markdown headings (``#``, ``##``), lists, tables,
          blockquotes, and horizontal rules (``---``).
        * For richer layout (centered hero sections, badges, columns, images
          with sizing) drop in HTML such as
          ``<div align="center"> ... </div>`` and ``<img src=... >``.
        * Uwazi also supports special components inside the body, e.g.
          ``{searchbox}}``, ``<Dataset />`` style widgets — only use these
          if the user asks; otherwise prefer plain Markdown/HTML.
        * Keep images referenced by absolute URLs.

    Do **not** pass a ``shared_id``: Uwazi mints it on creation and returns
    it to you. The optional ``javascript`` field populates the page's
    "Javascript" tab and runs on the public page; leave it empty unless the
    user asks for interactive behavior.

    Args:
        pages: The list of new pages to create. Each needs a ``title`` and a
            ``content`` body (Markdown/HTML).
        language: ISO 639-1 language code applied when a page does not set
            its own ``language``. Defaults to "en".

    Returns:
        A per-page result. On success, ``shared_id`` holds the new id and
        ``url`` the public link. On failure, ``shared_id`` is empty and
        ``error`` explains why. One failure does not abort the rest. On
        catastrophic error, returns a string describing the problem.
    """
    if ctx.deps.page_api is None:
        return "Error: Page tools are not configured: `page_api` is missing on dependencies."
    try:
        return await ctx.deps.page_api.create_pages(pages=pages, language=language)
    except DomainError as exc:
        logger.error("create_pages FAILED: {}", exc)
        return f"Error creating pages: {exc}. Please check the page data and retry."
