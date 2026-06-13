from loguru import logger
from pydantic_ai import RunContext

from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies
from uwazi_api.domain.exceptions import DomainError


async def get_relationship_type_names(
    ctx: RunContext[UwaziAgentToolsDependencies],
) -> list[str] | str:
    """List the names of all relationship types defined in the Uwazi instance.

    Relationship types name the meaning of a connection between two entities
    (e.g. "author of", "cited by", "related to"). You need one to define a
    ``relationship`` template property. Call this before creating a
    relationship property to check whether a suitable type already exists.

    Returns:
        The list of relationship type names. On error, returns a string
        describing the problem.
    """
    if ctx.deps.relationship_type_api is None:
        return "Error: Relationship type tools are not configured: `relationship_type_api` is missing on dependencies."
    try:
        return await ctx.deps.relationship_type_api.get_relationship_type_names()
    except DomainError as exc:
        logger.error("get_relationship_type_names FAILED: {}", exc)
        return f"Error listing relationship types: {exc}. Please check the Uwazi connection and retry."
