from pydantic_ai import RunContext

from uwazi_agent.domain.agent_entity_create import AgentEntityCreate
from uwazi_agent.domain.agent_entity_mutation_result import AgentEntityMutationResult
from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies


async def create_entities(
    ctx: RunContext[UwaziAgentToolsDependencies],
    entities: list[AgentEntityCreate],
    language: str = "en",
) -> list[AgentEntityMutationResult]:
    """Create one or more brand-new entities in Uwazi.

    Use this when the user wants to add new records (entities), not change
    existing ones. Each entity belongs to a template, and its ``metadata``
    shape is defined by that template's properties.

    Do **not** pass a ``shared_id``: it is minted by Uwazi on creation and
    returned to you in the result. To modify an entity that already exists,
    use ``update_entities`` instead.

    Shape and validation:
        * ``title`` and ``template_name`` are required. The template must
          already exist — look it up with ``get_templates_by_names`` (or
          create it with ``create_template``) first if unsure.
        * ``metadata`` keys must match the template's property names. Before
          filling metadata, inspect the template to learn each property's
          type and which thesaurus values are valid.
        * Pass thesaurus values (``select``/``multiselect``) as **labels**,
          never as UUIDs; the mapper resolves them.
        * Dates may be given as ISO strings; the mapper coerces them to the
          epoch form Uwazi expects.
        * Omit the platform-managed common properties (creationDate,
          editDate); they are handled automatically.

    Args:
        entities: The list of new entities to create.
        language: ISO 639-1 language code. Defaults to "en".

    Returns:
        A per-entity result. On success, ``shared_id`` holds the id Uwazi
        assigned to the newly created entity. On failure, ``shared_id`` is
        empty and ``error`` describes the problem (e.g. unknown template,
        invalid property name, invalid thesaurus label). One failure does
        not abort the rest.
    """
    if ctx.deps.entity_api is None:
        raise RuntimeError("Entity tools are not configured: `entity_api` is missing on dependencies.")
    return await ctx.deps.entity_api.create_entities(entities=entities, language=language)
