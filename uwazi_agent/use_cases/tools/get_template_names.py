from pydantic_ai import RunContext

from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies


async def get_template_names(
    ctx: RunContext[UwaziAgentToolsDependencies],
) -> list[str]:
    """List the names of all templates available in the Uwazi instance.

    Use this to discover what templates exist before the user asks to read,
    create, update or delete one.

    Returns:
        The list of template names currently defined.
    """
    return await ctx.deps.template_api.get_template_names()
