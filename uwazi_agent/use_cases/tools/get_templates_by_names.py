from pydantic_ai import RunContext

from uwazi_agent.domain.agent_template import AgentTemplate
from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies


async def get_templates_by_names(
    ctx: RunContext[UwaziAgentToolsDependencies],
    names: list[str],
) -> list[AgentTemplate]:
    """Look up templates by their human-readable name.

    Use this when the user references a template by name and you need its
    current properties. Names are matched exactly; unknown names are
    silently skipped from the result.

    The returned templates contain only the user-defined custom properties
    (not the platform-managed common properties).

    Args:
        names: The template names to look up.

    Returns:
        The matching templates with their custom properties.
    """
    return await ctx.deps.template_api.get_templates_by_names(names=names)
