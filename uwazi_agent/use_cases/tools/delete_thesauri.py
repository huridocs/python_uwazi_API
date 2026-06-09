from pydantic_ai import RunContext

from uwazi_agent.use_cases.tools.dependencies import ThesauriToolsDependencies


async def delete_thesauri(
    ctx: RunContext[ThesauriToolsDependencies],
    name: str,
    language: str = "en",
) -> dict:
    """Delete a thesaurus by name.

    Use this to remove a thesaurus that is no longer referenced by any
    entity. Uwazi only allows deletion of unassigned thesauri.

    Args:
        name: The name of the thesaurus to delete.
        language: ISO 639-1 language code. Defaults to "en".

    Returns:
        The API response payload for the deletion.
    """
    return await ctx.deps.api.delete_thesauri(name=name, language=language)
