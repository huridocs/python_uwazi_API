from pydantic_ai import RunContext

from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies


async def get_thesauris_names(
    ctx: RunContext[UwaziAgentToolsDependencies],
    language: str = "en",
) -> list[str]:
    """List the names of all thesauri available in the Uwazi instance.

    Use this to discover what controlled vocabularies exist before the user
    asks to read, create, update or delete one.

    Args:
        language: ISO 639-1 language code. Defaults to "en".

    Returns:
        The list of thesaurus names currently defined.
    """
    thesauris = await ctx.deps.thesauri_api.get_thesauris(language=language)
    return [t.name for t in thesauris]
