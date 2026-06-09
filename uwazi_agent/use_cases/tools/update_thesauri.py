from pydantic_ai import RunContext

from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies


async def update_thesauri(
    ctx: RunContext[UwaziAgentToolsDependencies],
    name: str,
    values: list[str],
    language: str = "en",
) -> dict:
    """Add the given value labels to an existing thesaurus.

    Existing values are kept; new labels are added. To remove a value, ask
    the user to do it manually through the Uwazi UI.

    Args:
        name: The thesaurus name to update.
        values: Value labels to ensure exist in the thesaurus.
        language: ISO 639-1 language code. Defaults to "en".

    Returns:
        The API response payload for the update.
    """
    return await ctx.deps.thesauri_api.update_thesauri(name=name, values=values, language=language)
