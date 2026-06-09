from pydantic_ai import RunContext

from uwazi_agent.use_cases.tools.dependencies import ThesauriToolsDependencies


async def update_thesauri(
    ctx: RunContext[ThesauriToolsDependencies],
    name: str,
    values: list[str],
    language: str = "en",
) -> dict:
    """Add new value labels to an existing thesaurus.

    Use this when the user wants to extend a thesaurus with additional
    options. Existing labels are preserved; only new labels are added.

    Args:
        name: The name of the thesaurus to update.
        values: New value labels to add. Labels that already exist are kept
            as-is and not duplicated.
        language: ISO 639-1 language code. Defaults to "en".

    Returns:
        The API response payload for the updated thesaurus.
    """
    return await ctx.deps.api.update_thesauri(name=name, values=values, language=language)
