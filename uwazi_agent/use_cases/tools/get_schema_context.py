from pydantic_ai import RunContext

from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies


async def get_schema_context(
    ctx: RunContext[UwaziAgentToolsDependencies],
    level: str = "full",
) -> str:
    """Return the schema store's prompt context at the requested detail level.

    A minimal schema index (template names, entity counts, languages,
    relationship-type names, thesaurus names) is already pre-loaded in your
    system prompt; you do not need to call this tool just to discover what
    templates exist. Call this tool only when you need more detail than
    that index provides.

    Levels:
        * ``minimal`` — same as the pre-loaded index, returned as a string.
        * ``full`` — adds every template's property formats, required/filter/
          card flags, thesaurus references, and all thesaurus values and
          groups. Use this when you need to read or mutate schema details
          (the schema agent should call this at the start of its run).

    Args:
        level: Either ``"minimal"`` or ``"full"``. Defaults to ``"full"``.

    Returns:
        The schema context string at the requested level, or an error
        message if ``level`` is not recognised.
    """
    if level not in ("minimal", "full"):
        return f"Error: level must be 'minimal' or 'full', got {level!r}."
    return ctx.deps.schema_store.to_prompt_context(level=level)
