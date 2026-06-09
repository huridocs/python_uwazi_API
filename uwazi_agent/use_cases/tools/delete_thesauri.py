from pydantic_ai import RunContext

from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies


async def delete_thesauri(
    ctx: RunContext[UwaziAgentToolsDependencies],
    name: str,
    language: str = "en",
) -> dict:
    """Delete a thesaurus by its human-readable name.

    The thesaurus must not be in use by any template, property, or entity —
    otherwise the Uwazi instance will reject the deletion.

    Args:
        name: The thesaurus name to delete.
        language: ISO 639-1 language code. Defaults to "en".

    Returns:
        The API response payload for the deletion.
    """
    return await ctx.deps.thesauri_api.delete_thesauri(name=name, language=language)
