from loguru import logger
from pydantic_ai import RunContext

from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies
from uwazi_agent.use_cases.tools.fail_forward import suggest_template_names
from uwazi_api.domain.exceptions import DomainError


async def delete_template(
    ctx: RunContext[UwaziAgentToolsDependencies],
    name: str,
    language: str = "en",
) -> dict | str:
    """Delete a template by its human-readable name.

    The template must be empty (no entities assigned to it) for Uwazi to
    accept the deletion. Use the entity tools to find and reassign or
    delete entities first.

    Args:
        name: The template name to delete.
        language: ISO 639-1 language code. Defaults to "en".

    Returns:
        The API response payload for the deletion. On error, returns a
        string with suggestions.
    """
    try:
        result = await ctx.deps.template_api.delete_template(name=name, language=language)
        # Re-fetch the template names list (and counts) so the "Available
        # context" block in the prompt reflects the deletion.
        from uwazi_agent.use_cases.tools.tool_context import refresh_templates
        await refresh_templates(ctx)
        return result
    except DomainError as exc:
        logger.error("delete_template FAILED: name={} error={}", name, exc)
        if "not found" in str(exc).lower():
            return await suggest_template_names(ctx.deps, name)
        return (
            f"Error deleting template '{name}': {exc}. The template may still have entities — delete them first, then retry."
        )
