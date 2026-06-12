from loguru import logger
from pydantic_ai import RunContext

from uwazi_agent.domain.agent_page_create import AgentPageCreate
from uwazi_agent.domain.agent_page_mutation_result import AgentPageMutationResult
from uwazi_agent.drivers.page_builder.renderer import PageRenderer
from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies
from uwazi_agent.use_cases.tools.render_page_from_blocks import _resolve_vibe
from uwazi_api.domain.exceptions import DomainError


async def create_page_from_blocks(
    ctx: RunContext[UwaziAgentToolsDependencies],
    title: str,
    blocks: list[dict],
    vibe: str | None = None,
    language: str = "en",
) -> list[AgentPageMutationResult] | str:
    """Create a new Settings → Page in Uwazi by composing pre-styled blocks.

    **This is the default way to create new pages.** The page is rendered
    from a curated set of block types (hero, content, stats_grid, card_grid,
    timeline, two_column, pie_chart, bar_chart, cta, divider) styled with a
    named visual theme (vibe). The result is a polished, self-contained HTML
    page — no raw HTML or CSS to write.

    **When NOT to use this tool:** if the user explicitly asks for fully
    custom HTML, CSS, or JavaScript that cannot be expressed with the block
    library, use ``create_pages`` instead.

    **Workflow:**
        1. ``list_page_blocks`` to learn the available block types and their
           slot schemas.
        2. ``list_page_vibes`` to see the available visual themes.
        3. ``render_page_from_blocks(blocks, vibe)`` to preview the HTML
           (optional but recommended).
        4. ``create_page_from_blocks(title, blocks, vibe, language)`` to push
           the page into Uwazi.

    **Default vibe:** if you don't pass a ``vibe`` (or pass an empty string),
    the ``minimal`` style is used (clean editorial, black/white, monochrome —
    a safe default that fits academic, archival, and most documentation
    content). Choose a different vibe when the page's tone calls for it
    (e.g. ``activist`` for urgent action alerts, ``earth`` for environmental
    data, ``warm`` for community programs, ``ocean`` for water/maritime
    topics, ``corporate`` for institutional reports).

    Args:
        title: The page's title. Used for the page header and as the
            Settings → Pages entry label.
        blocks: An ordered list of block definitions. Each block has a
            ``type`` (from ``list_page_blocks``) and a ``slots`` dict whose
            shape matches the block's slot schema.
        vibe: Optional visual theme name. If ``None`` or empty, ``minimal``
            is used.
        language: ISO 639-1 language code (e.g. ``en``, ``fr``, ``es``).
            Defaults to ``en``.

    Returns:
        A per-page result. On success, ``shared_id`` holds the new id and
        ``url`` the public link. On failure, ``shared_id`` is empty and
        ``error`` explains why. On configuration / rendering error, returns
        a string beginning with ``Error:``.
    """
    if ctx.deps.page_api is None:
        return "Error: Page tools are not configured: `page_api` is missing on dependencies."
    if ctx.deps.page_builder_dir is None:
        return "Error: Page builder is not configured: `page_builder_dir` is missing on dependencies."

    resolved_vibe = _resolve_vibe(vibe)
    renderer = PageRenderer(ctx.deps.page_builder_dir)
    try:
        html = renderer.render(vibe=resolved_vibe, blocks=blocks)
    except ValueError as exc:
        return f"Error rendering page: {exc}"
    except Exception as exc:
        return f"Error rendering page: {exc}. Check block slot values against the schemas from list_page_blocks."

    page = AgentPageCreate(
        title=title,
        content=html,
        language=language,
        entity_view=False,
    )
    try:
        return await ctx.deps.page_api.create_pages(pages=[page], language=language)
    except DomainError as exc:
        logger.error("create_page_from_blocks FAILED: {}", exc)
        return f"Error creating page: {exc}. The HTML was rendered but Uwazi rejected the create request."
