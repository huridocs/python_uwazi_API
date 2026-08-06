from loguru import logger
from pydantic_ai import RunContext

from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies
from uwazi_api.domain.exceptions import DomainError


async def list_thesauri(
    ctx: RunContext[UwaziAgentToolsDependencies],
    language: str = "en",
) -> list[str] | str:
    """List the names of all thesauri available in the Uwazi instance.

    Use this to discover what controlled vocabularies exist before the user
    asks to read, create, update or delete one. To inspect a thesaurus's
    values and how heavily each one is used, follow up with
    ``get_thesauris_by_names``.

    Args:
        language: ISO 639-1 language code. Defaults to "en".

    Returns:
        The list of thesaurus names currently defined. On error, returns
        a string describing the problem.
    """
    if ctx.deps.schema_store.thesauri_names:
        return ctx.deps.schema_store.thesauri_names
    try:
        thesauris = await ctx.deps.thesauri_api.get_thesauris(language=language)
        names = [t.name for t in thesauris]
        ctx.deps.schema_store.add_thesauri_names(names)
        ctx.deps.schema_store.add_thesauri(thesauris)
        return names
    except DomainError as exc:
        logger.error("list_thesauri FAILED: {}", exc)
        return f"Error listing thesauri: {exc}. Please check the Uwazi connection and retry."
