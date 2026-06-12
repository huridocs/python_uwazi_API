from typing import Any

from pydantic_ai import RunContext

from uwazi_agent.drivers.page_builder.renderer import PageRenderer
from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies

DEFAULT_VIBE: str = "minimal"


def _resolve_vibe(vibe: str | None) -> str:
    if vibe is None or not vibe.strip():
        return DEFAULT_VIBE
    return vibe.strip().lower()


async def render_page_from_blocks(
    ctx: RunContext[UwaziAgentToolsDependencies],
    blocks: list[dict[str, Any]],
    vibe: str | None = None,
) -> str:
    """Render a list of page blocks with a chosen vibe into a self-contained HTML string.

    This is a **preview** tool — it does NOT create a page in Uwazi. Use it to
    validate that your blocks, slot values, and vibe choice render correctly
    before calling the page-creation tool. The output is a complete HTML
    document (no external dependencies) that you can inspect.

    **Workflow for creating a new page:**
        1. ``list_page_blocks`` to discover available blocks and their slot
           schemas.
        2. ``list_page_vibes`` to pick a visual theme (or omit to use
           ``minimal`` by default).
        3. ``render_page_from_blocks(blocks, vibe)`` to preview the HTML.
        4. ``create_page_from_blocks(title, blocks, vibe, language)`` to push
           the rendered HTML into Uwazi and register the page.

    Args:
        blocks: An ordered list of block definitions. Each block has a
            ``type`` (from ``list_page_blocks``) and a ``slots`` dict whose
            shape matches the block's slot schema.
        vibe: The visual theme name. If ``None`` or empty, ``minimal`` is
            used (clean editorial style, safe default for most content).

    Returns:
        The full HTML document on success. On validation or rendering error,
        returns a string beginning with ``Error:`` describing what to fix.
    """
    if ctx.deps.page_builder_dir is None:
        return "Error: Page builder is not configured: `page_builder_dir` is missing on dependencies."
    resolved = _resolve_vibe(vibe)
    renderer = PageRenderer(ctx.deps.page_builder_dir)
    try:
        return renderer.render(vibe=resolved, blocks=blocks)
    except ValueError as exc:
        return f"Error rendering page: {exc}"
    except Exception as exc:
        return f"Error rendering page: {exc}. Check block slot values against the schemas from list_page_blocks."
