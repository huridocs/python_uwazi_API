from typing import Any

from pydantic_ai import RunContext

from uwazi_agent.drivers.page_builder.registry import BlockRegistry
from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies


def _get_block_registry(ctx: RunContext[UwaziAgentToolsDependencies]) -> BlockRegistry:
    if ctx.deps.page_builder_dir is None:
        raise ValueError("Error: Page builder is not configured: `page_builder_dir` is missing on dependencies.")
    return BlockRegistry(ctx.deps.page_builder_dir / "blocks")


async def list_page_blocks(
    ctx: RunContext[UwaziAgentToolsDependencies],
) -> list[dict[str, Any]] | str:
    """List every block type available in the page-template system.

    Each block is a pre-styled, self-contained section (hero, content, stats_grid,
    card_grid, timeline, two_column, pie_chart, bar_chart, cta, divider). You compose
    a page by ordering blocks and filling their ``slots`` with content.

    Call this FIRST when the user asks for a new page so you know what building
    blocks you have. Each entry includes the block's name, description,
    ``when_to_use`` guidance, and the full slot schema (required and optional).
    You do NOT need this tool for editing existing pages — fetch them with
    ``get_pages_by_shared_ids`` instead.

    Returns:
        A list of block descriptors. On configuration error, returns a string
        describing the problem.
    """
    try:
        registry = _get_block_registry(ctx)
    except ValueError as exc:
        return str(exc)
    return registry.list_blocks()
