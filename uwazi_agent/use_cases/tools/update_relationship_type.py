from loguru import logger
from pydantic_ai import RunContext

from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies
from uwazi_api.domain.exceptions import DomainError


async def update_relationship_type(
    ctx: RunContext[UwaziAgentToolsDependencies],
    name: str,
    new_name: str,
    language: str = "en",
) -> dict | str:
    """Rename an existing relationship type.

    Identify the relationship type by its current ``name`` and give the
    ``new_name`` to apply. Templates that already reference this type keep
    working — only the displayed name changes.

    Args:
        name: The current name of the relationship type.
        new_name: The new name to apply.
        language: ISO 639-1 language code. Defaults to "en".

    Returns:
        The API response payload for the update. On error, returns a string
        describing the problem.
    """
    if ctx.deps.relationship_type_api is None:
        return "Error: Relationship type tools are not configured: `relationship_type_api` is missing on dependencies."
    try:
        return await ctx.deps.relationship_type_api.update_relationship_type(name=name, new_name=new_name, language=language)
    except DomainError as exc:
        logger.error("update_relationship_type FAILED: name={} new_name={} error={}", name, new_name, exc)
        return (
            f"Error updating relationship type '{name}': {exc}. "
            "Use get_relationship_type_names to see existing types and retry."
        )
