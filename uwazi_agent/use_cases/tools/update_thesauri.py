from loguru import logger
from pydantic_ai import RunContext

from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies
from uwazi_agent.use_cases.tools.fail_forward import suggest_thesauri_names
from uwazi_api.domain.exceptions import DomainError


async def update_thesauri(
    ctx: RunContext[UwaziAgentToolsDependencies],
    name: str,
    values: list[str],
    language: str = "en",
) -> dict | str:
    logger.info("update_thesauri(name={!r}, values_count={}, language={!r})", name, len(values), language)
    """Add the given value labels to an existing thesaurus.

    Existing values are kept; new labels are added. To remove a value, ask
    the user to do it manually through the Uwazi UI.

    Args:
        name: The thesaurus name to update.
        values: Value labels to ensure exist in the thesaurus.
        language: ISO 639-1 language code. Defaults to "en".

    Returns:
        The API response payload for the update. On error, returns a
        string with suggestions.
    """
    try:
        return await ctx.deps.thesauri_api.update_thesauri(name=name, values=values, language=language)
    except DomainError as exc:
        if "not found" in str(exc).lower():
            return await suggest_thesauri_names(ctx.deps, name, language)
        return f"Error updating thesaurus '{name}': {exc}. Please check the thesaurus name and values, then retry."
