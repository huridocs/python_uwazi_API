from pydantic_ai import RunContext

from uwazi_agent.drivers.page_builder.registry import VibeRegistry
from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies


async def list_page_vibes(
    ctx: RunContext[UwaziAgentToolsDependencies],
) -> list[str] | str:
    """List the available visual themes (vibes) for page blocks.

    A vibe is a named set of design tokens (colors, fonts, spacing, radii) that
    every block in a page pulls from. Pick the vibe that matches the page's
    tone. If the user did not request a specific style, pass nothing to the
    page-creation tool and the default ``minimal`` vibe will be used.

    Available vibes include editorial/academic, corporate, activist/urgent,
    earth/environmental, ocean, and warm/community.

    Returns:
        The list of vibe names. On configuration error, returns a string
        describing the problem.
    """
    if ctx.deps.page_builder_dir is None:
        return "Error: Page builder is not configured: `page_builder_dir` is missing on dependencies."
    registry = VibeRegistry(ctx.deps.page_builder_dir / "vibes")
    return registry.list_vibes()
