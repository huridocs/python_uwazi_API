from loguru import logger
from pydantic_ai import RunContext

from uwazi_agent.domain.agent_thesauri import AgentThesauriGroup
from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies
from uwazi_agent.use_cases.tools.fail_forward import suggest_thesauri_names
from uwazi_api.domain.exceptions import DomainError


async def update_thesauri(
    ctx: RunContext[UwaziAgentToolsDependencies],
    name: str,
    values: list[str],
    language: str = "en",
    groups: list[AgentThesauriGroup] | None = None,
) -> dict | str:
    """Add value labels and/or groups to an existing thesaurus (additive merge).

    This is additive: existing values and groups are preserved (their ids stay
    stable so entities that reference them are not broken). New top-level labels
    in ``values`` are appended. For each entry in ``groups``, a group with the
    same name is extended with any new child labels, or created if it does not
    exist yet. To remove a value, ask the user to do it through the Uwazi UI.

    Args:
        name: The thesaurus name to update.
        values: Top-level value labels to ensure exist in the thesaurus.
        language: ISO 639-1 language code. Defaults to "en".
        groups: Optional named groups to add or extend, each with its own
            child value labels.

    Returns:
        The API response payload for the update. On error, returns a
        string with suggestions.
    """
    try:
        result = await ctx.deps.thesauri_api.update_thesauri(name=name, values=values, language=language, groups=groups)
        # Re-fetch the cached thesaurus names so the "Available context"
        # block in the prompt reflects the change.
        from uwazi_agent.use_cases.tools.tool_context import refresh_thesauri_names
        await refresh_thesauri_names(ctx)
        return result
    except DomainError as exc:
        logger.error("update_thesauri FAILED: name={} error={}", name, exc)
        if "not found" in str(exc).lower():
            return await suggest_thesauri_names(ctx.deps, name, language)
        return f"Error updating thesaurus '{name}': {exc}. Please check the thesaurus name and values, then retry."
