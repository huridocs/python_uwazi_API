from loguru import logger
from pydantic_ai import RunContext

from uwazi_agent.domain.agent_thesauri import AgentThesauri
from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies
from uwazi_api.domain.exceptions import DomainError


async def get_thesauris_by_names(
    ctx: RunContext[UwaziAgentToolsDependencies],
    names: list[str],
    language: str = "en",
) -> list[AgentThesauri] | str:
    logger.info("get_thesauris_by_names(names={!r}, language={!r})", names, language)
    """Look up thesauri by their human-readable name.

    Use this when the user references a thesaurus by name (e.g. "Countries",
    "Languages", "Topics") and you need its current values. Names are matched
    exactly; unknown names are silently skipped from the result.

    Args:
        names: The thesaurus names to look up.
        language: ISO 639-1 language code for the values. Defaults to "en".

    Returns:
        The matching thesauri, each with its current list of value labels.
        On error, returns a string describing the problem.
    """
    try:
        return await ctx.deps.thesauri_api.get_thesauris_by_names(names=names, language=language)
    except DomainError as exc:
        return f"Error looking up thesauri: {exc}. Use get_thesauris_names to see available thesauri and retry."
