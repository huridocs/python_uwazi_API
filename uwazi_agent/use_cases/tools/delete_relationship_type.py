from loguru import logger
from pydantic_ai import RunContext

from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies
from uwazi_api.domain.exceptions import DomainError


async def delete_relationship_type(
    ctx: RunContext[UwaziAgentToolsDependencies],
    name: str,
    language: str = "en",
) -> dict | str:
    """Delete a relationship type by name.

    A relationship type can only be removed when no template property still
    references it; otherwise Uwazi rejects the deletion. Always confirm with
    the user first, since templates and existing relationships depend on it.

    Args:
        name: The name of the relationship type to delete.
        language: ISO 639-1 language code. Defaults to "en".

    Returns:
        The API response payload for the deletion. On error (e.g. the type is
        still in use), returns a string describing the problem.
    """
    if ctx.deps.relationship_type_api is None:
        return "Error: Relationship type tools are not configured: `relationship_type_api` is missing on dependencies."
    try:
        return await ctx.deps.relationship_type_api.delete_relationship_type(name=name, language=language)
    except DomainError as exc:
        logger.error("delete_relationship_type FAILED: name={} error={}", name, exc)
        return (
            f"Error deleting relationship type '{name}': {exc}. "
            "It may still be referenced by a template property. Use get_relationship_type_names to verify."
        )
