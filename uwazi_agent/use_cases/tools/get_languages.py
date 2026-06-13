from loguru import logger
from pydantic_ai import RunContext

from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies
from uwazi_api.domain.exceptions import DomainError
from uwazi_api.domain.language import Language


async def get_languages(
    ctx: RunContext[UwaziAgentToolsDependencies],
) -> list[Language] | str:
    """List all languages configured in the Uwazi instance.

    Use this to discover which languages are available before working with
    entities, thesauri, templates or pages in a specific language. Each
    language has a ``key`` (ISO 639-1 code like ``en``, ``es``, ``fr``),
    a human-readable ``label``, and a ``default`` flag indicating the
    instance's default language.

    Returns:
        The list of configured languages. On error, returns a string
        describing the problem.
    """
    if ctx.deps.settings_api is None:
        return "Error: Settings tools are not configured: `settings_api` is missing on dependencies."
    try:
        return await ctx.deps.settings_api.get_languages()
    except DomainError as exc:
        logger.error("get_languages FAILED: {}", exc)
        return f"Error listing languages: {exc}. Please check the Uwazi connection and retry."
