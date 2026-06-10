from loguru import logger
from pydantic_ai import RunContext

from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies
from uwazi_api.domain.exceptions import DomainError


async def get_template_names(
    ctx: RunContext[UwaziAgentToolsDependencies],
) -> list[str] | str:
    logger.info("get_template_names()")
    """List the names of all templates available in the Uwazi instance.

    Use this to discover what templates exist before the user asks to read,
    create, update or delete one.

    Returns:
        The list of template names currently defined. On error, returns
        a string describing the problem.
    """
    if ctx.deps.schema_store.template_names:
        return ctx.deps.schema_store.template_names
    try:
        names = await ctx.deps.template_api.get_template_names()
        ctx.deps.schema_store.add_template_names(names)
        return names
    except DomainError as exc:
        return f"Error listing template names: {exc}. Please check the Uwazi connection and retry."
