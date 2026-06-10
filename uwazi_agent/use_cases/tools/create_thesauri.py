from loguru import logger
from pydantic_ai import RunContext

from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies
from uwazi_api.domain.exceptions import DomainError


async def create_thesauri(
    ctx: RunContext[UwaziAgentToolsDependencies],
    name: str,
    values: list[str],
    language: str = "en",
) -> dict | str:
    logger.info("create_thesauri(name={!r}, values_count={}, language={!r})", name, len(values), language)
    """Create a new thesaurus with the given values.

    Use this when the user wants to add a brand-new controlled vocabulary.
    Fails if a thesaurus with the same name already exists.

    Args:
        name: The unique name for the new thesaurus.
        values: Initial value labels to seed the thesaurus with.
        language: ISO 639-1 language code. Defaults to "en".

    Returns:
        The API response payload for the created thesaurus. On error,
        returns a string describing the problem.
    """
    try:
        return await ctx.deps.thesauri_api.create_thesauri(name=name, values=values, language=language)
    except DomainError as exc:
        return f"Error creating thesaurus '{name}': {exc}. Use get_thesauris_names to check existing thesauri and retry."
