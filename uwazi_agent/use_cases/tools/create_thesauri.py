from pydantic_ai import RunContext

from uwazi_agent.use_cases.tools.dependencies import ThesauriToolsDependencies


async def create_thesauri(
    ctx: RunContext[ThesauriToolsDependencies],
    name: str,
    values: list[str],
    language: str = "en",
) -> dict:
    """Create a new thesaurus with the given values.

    Use this when the user wants to add a brand-new controlled vocabulary.
    Fails if a thesaurus with the same name already exists.

    Args:
        name: The unique name for the new thesaurus.
        values: Initial value labels to seed the thesaurus with.
        language: ISO 639-1 language code. Defaults to "en".

    Returns:
        The API response payload for the created thesaurus.
    """
    return await ctx.deps.api.create_thesauri(name=name, values=values, language=language)
