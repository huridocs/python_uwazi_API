from pydantic_ai import RunContext

from uwazi_agent.domain.agent_entity import AgentEntity
from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies


async def get_entities_by_shared_ids(
    ctx: RunContext[UwaziAgentToolsDependencies],
    shared_ids: list[str],
    language: str = "en",
) -> list[AgentEntity]:
    """Fetch full entity details by their Uwazi ``shared_id``.

    Use this when you already know the stable ``shared_id`` of one or more
    entities and want to inspect their full metadata. Never invent ids; if you
    only have a title or a free-form description, first call
    ``search_entities_by_text`` to discover the ``shared_id``.

    Entity identification rules:
        * ``shared_id`` is the only stable identifier. Titles are not unique
          and may change.
        * Two entities can share the same title; only ``shared_id``
          disambiguates them.
        * Identifiers returned by ``search_entities_by_text`` are guaranteed
          to be valid; reuse them as input to this tool.

    The returned entities have their template id resolved back to a
    human-readable ``template_name`` and select/multiselect values resolved
    back to thesaurus labels, so you can reason about them in plain language.
    The heavy ``documents`` and ``attachments`` payloads are stripped because
    they are not useful for reasoning and would bloat the context.

    Args:
        shared_ids: The list of Uwazi shared ids to look up. Unknown ids are
            silently skipped from the result.
        language: ISO 639-1 language code. Defaults to "en".

    Returns:
        The matching entities with their full metadata, labels resolved.
    """
    if ctx.deps.entity_api is None:
        raise RuntimeError("Entity tools are not configured: `entity_api` is missing on dependencies.")
    return await ctx.deps.entity_api.get_entities_by_shared_ids(shared_ids=shared_ids, language=language)
