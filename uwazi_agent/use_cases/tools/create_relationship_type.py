from loguru import logger
from pydantic_ai import RunContext

from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies
from uwazi_api.domain.exceptions import DomainError


async def create_relationship_type(
    ctx: RunContext[UwaziAgentToolsDependencies],
    name: str,
    language: str = "en",
) -> dict | str:
    logger.info("create_relationship_type(name={!r}, language={!r})", name, language)
    """Create a new relationship type.

    A relationship type is the labelled, reusable kind of connection that a
    ``relationship`` template property uses (e.g. "author of", "related to").
    Create one when no existing type fits the connection you want to model,
    then reference it by name from the property's ``relationship_type_name``.

    Args:
        name: The unique name for the new relationship type. Prefer a short
            verb phrase that reads naturally ("cites", "located in").
        language: ISO 639-1 language code. Defaults to "en".

    Returns:
        The API response payload for the created relationship type. On error,
        returns a string describing the problem.
    """
    if ctx.deps.relationship_type_api is None:
        return "Error: Relationship type tools are not configured: `relationship_type_api` is missing on dependencies."
    try:
        return await ctx.deps.relationship_type_api.create_relationship_type(name=name, language=language)
    except DomainError as exc:
        return (
            f"Error creating relationship type '{name}': {exc}. "
            "Use get_relationship_type_names to check existing types and retry."
        )
