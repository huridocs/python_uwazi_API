from loguru import logger
from pydantic_ai import RunContext

from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies
from uwazi_agent.use_cases.tools.fail_forward import suggest_thesauri_names
from uwazi_api.domain.exceptions import DomainError


async def delete_thesauri(
    ctx: RunContext[UwaziAgentToolsDependencies],
    name: str,
    language: str = "en",
) -> dict | str:
    logger.info("delete_thesauri(name={!r}, language={!r})", name, language)
    """Delete a thesaurus by its human-readable name.

    The thesaurus must not be in use by any template, property, or entity —
    otherwise the Uwazi instance will reject the deletion.

    Args:
        name: The thesaurus name to delete.
        language: ISO 639-1 language code. Defaults to "en".

    Returns:
        The API response payload for the deletion. On error, returns a
        string with suggestions.
    """
    try:
        return await ctx.deps.thesauri_api.delete_thesauri(name=name, language=language)
    except DomainError as exc:
        logger.error("delete_thesauri FAILED: name={} error={}", name, exc)
        if "not found" in str(exc).lower():
            return await suggest_thesauri_names(ctx.deps, name, language)
        return f"Error deleting thesaurus '{name}': {exc}. The thesaurus may be in use by a template — remove the reference first, then retry."
