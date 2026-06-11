from loguru import logger
from pydantic_ai import RunContext

from uwazi_agent.configuration import ENTITIES_LIMIT_FOR_LLM_MODEL
from uwazi_agent.domain.agent_entity import AgentEntity
from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies
from uwazi_api.domain.exceptions import DomainError


async def get_entities_by_shared_ids(
    ctx: RunContext[UwaziAgentToolsDependencies],
    shared_ids: list[str],
    language: str = "en",
    limit: int = 10000,
) -> list[AgentEntity] | str:
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

    Entity store:
        * All fetched entities are automatically stored in the session entity
          store for batch processing via the Python agent.
        * When the result count exceeds ``ENTITIES_LIMIT_FOR_LLM_MODEL``,
          suggest using the Python agent for processing instead of handling
          entities individually.

    Args:
        shared_ids: The list of Uwazi shared ids to look up. Unknown ids are
            silently skipped from the result.
        language: ISO 639-1 language code. Defaults to "en".

    Returns:
        The matching entities with their full metadata, labels resolved.
        On error, returns a string describing the problem.
    """
    if ctx.deps.entity_api is None:
        return "Error: Entity tools are not configured: `entity_api` is missing on dependencies."
    try:
        entities = await ctx.deps.entity_api.get_entities_by_shared_ids(
            shared_ids=shared_ids, language=language, limit=limit
        )
    except DomainError as exc:
        logger.error("get_entities_by_shared_ids FAILED: shared_ids={} error={}", shared_ids, exc)
        return f"Error fetching entities by shared_ids: {exc}. Please verify the shared_ids and retry."
    ctx.deps.entity_store.add_entities(entities)
    if len(entities) > ENTITIES_LIMIT_FOR_LLM_MODEL:
        ctx.deps.entity_store.needs_python_agent = True
        return (
            f"{len(entities)} entities fetched and stored in the entity store. "
            f"This exceeds the LLM limit of {ENTITIES_LIMIT_FOR_LLM_MODEL}. "
            f"You MUST delegate to the Python agent for batch processing."
        )
    return entities
