from pydantic_ai import RunContext

from uwazi_agent.domain.agent_thesauri import AgentThesauri
from uwazi_agent.use_cases.tools.dependencies import ThesauriToolsDependencies


async def get_thesauris_by_names(
    ctx: RunContext[ThesauriToolsDependencies],
    names: list[str],
    language: str = "en",
) -> list[AgentThesauri]:
    """Look up thesauri by their human-readable name.

    Use this when the user references a thesaurus by name (e.g. "Countries",
    "Languages", "Topics") and you need its current values. Names are matched
    exactly; unknown names are silently skipped from the result.

    Args:
        names: The thesaurus names to look up.
        language: ISO 639-1 language code for the values. Defaults to "en".

    Returns:
        The matching thesauri, each with its current list of value labels.
    """
    return await ctx.deps.api.get_thesauris_by_names(names=names, language=language)
