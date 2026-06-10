from loguru import logger
from pydantic_ai import RunContext

from uwazi_agent.domain.agent_thesauri import AgentThesauriGroup
from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies
from uwazi_api.domain.exceptions import DomainError


async def create_thesauri(
    ctx: RunContext[UwaziAgentToolsDependencies],
    name: str,
    values: list[str],
    language: str = "en",
    groups: list[AgentThesauriGroup] | None = None,
) -> dict | str:
    logger.info(
        "create_thesauri(name={!r}, values_count={}, groups_count={}, language={!r})",
        name,
        len(values),
        len(groups or []),
        language,
    )
    """Create a new thesaurus (controlled vocabulary), optionally with groups.

    Use this when the user wants to add a brand-new controlled vocabulary.
    Fails if a thesaurus with the same name already exists.

    Thesauri may organise values into groups. A group has a ``name`` (heading)
    and its own list of child value ``values``. Use ``values`` for flat,
    top-level options and ``groups`` for grouped options; a thesaurus may use
    either or both. Only the children of a group are selectable in
    select/multiselect properties — the group name is just a heading.

    Args:
        name: The unique name for the new thesaurus.
        values: Top-level (ungrouped) value labels to seed the thesaurus with.
        language: ISO 639-1 language code. Defaults to "en".
        groups: Optional named groups, each with its own child value labels,
            e.g. ``[{"name": "Europe", "values": ["Spain", "France"]}]``.

    Returns:
        The API response payload for the created thesaurus. On error,
        returns a string describing the problem.
    """
    try:
        return await ctx.deps.thesauri_api.create_thesauri(name=name, values=values, language=language, groups=groups)
    except DomainError as exc:
        logger.error("create_thesauri FAILED: name={} error={}", name, exc)
        return f"Error creating thesaurus '{name}': {exc}. Use get_thesauris_names to check existing thesauri and retry."
