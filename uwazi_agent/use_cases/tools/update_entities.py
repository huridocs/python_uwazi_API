from pydantic_ai import RunContext

from uwazi_agent.domain.agent_entity import AgentEntity
from uwazi_agent.domain.agent_entity_mutation_result import AgentEntityMutationResult
from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies


async def update_entities(
    ctx: RunContext[UwaziAgentToolsDependencies],
    updates: list[AgentEntity],
    language: str = "en",
) -> list[AgentEntityMutationResult]:
    """Apply partial updates to one or more existing entities.

    This is a *partial merge*: only the metadata fields you provide are
    changed; everything else (including metadata fields you omit) is
    preserved on Uwazi's side. To clear a metadata field, first look up
    the entity with ``get_entities_by_shared_ids`` and decide explicitly;
    there is no implicit "set to null" semantics.

    Identification and shape:
        * Each update must include a ``shared_id``. Look it up first with
          ``search_entities_by_text`` if you only have a title.
        * ``template_name`` is required so the mapper can coerce metadata
          values to the right Uwazi shape (e.g. dates to epoch, select
          labels to UUIDs, geolocation to ``{lat, lon}``).
        * Pass thesaurus values as **labels**, not UUIDs; the mapper
          resolves them.
        * To change the template of an existing entity, pass the new
          ``template_name``. The new template's required properties must
          be present in ``metadata`` (omitted fields will be left blank).

    Args:
        updates: The list of partial entity updates to apply.
        language: ISO 639-1 language code. Defaults to "en".

    Returns:
        A per-entity result indicating success or a descriptive error
        (e.g. unknown shared_id, unknown template, invalid thesaurus
        label). Failures in one entity do not abort the rest.
    """
    if ctx.deps.entity_api is None:
        raise RuntimeError("Entity tools are not configured: `entity_api` is missing on dependencies.")
    return await ctx.deps.entity_api.update_entities(updates=updates, language=language)
