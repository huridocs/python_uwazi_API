from loguru import logger
from pydantic_ai import RunContext

from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies
from uwazi_api.domain.exceptions import DomainError


async def create_relationship_type(
    ctx: RunContext[UwaziAgentToolsDependencies],
    name: str,
    language: str = "en",
) -> dict | str:
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
        result = await ctx.deps.relationship_type_api.create_relationship_type(name=name, language=language)
        # Re-fetch the cached relationship type names so the "Available
        # context" block in the prompt reflects the new type.
        from uwazi_agent.use_cases.tools.tool_context import refresh_relationship_type_names

        await refresh_relationship_type_names(ctx)
        return result
    except DomainError as exc:
        logger.error("create_relationship_type FAILED: name={} error={}", name, exc)
        return (
            f"Error creating relationship type '{name}': {exc}. "
            "Use get_relationship_type_names to check existing types and retry."
        )
