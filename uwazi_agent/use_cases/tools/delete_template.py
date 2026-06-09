from pydantic_ai import RunContext

from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies


async def delete_template(
    ctx: RunContext[UwaziAgentToolsDependencies],
    name: str,
    language: str = "en",
) -> dict:
    """Delete a template by its human-readable name.

    The template must be empty (no entities assigned to it) for Uwazi to
    accept the deletion. Use the entity tools to find and reassign or
    delete entities first.

    Args:
        name: The template name to delete.
        language: ISO 639-1 language code. Defaults to "en".

    Returns:
        The API response payload for the deletion.
    """
    return await ctx.deps.template_api.delete_template(name=name, language=language)
