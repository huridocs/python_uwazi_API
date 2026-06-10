from pydantic_ai import RunContext

from uwazi_agent.domain.agent_entity_create import AgentEntityCreate
from uwazi_agent.domain.agent_entity_mutation_result import AgentEntityMutationResult
from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies
from uwazi_agent.use_cases.tools.fail_forward import suggest_template_names
from uwazi_api.domain.exceptions import DomainError


async def create_entities(
    ctx: RunContext[UwaziAgentToolsDependencies],
    entities: list[AgentEntityCreate],
    language: str = "en",
) -> list[AgentEntityMutationResult] | str:
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
          type and which thesaurus values are valid. Each property carries a
          ``format_instructions`` string — follow it exactly.
        * Pass thesaurus values (``select``/``multiselect``) as **labels**,
          never as UUIDs; the mapper resolves them.
        * Dates may be given as ISO strings; the mapper coerces them to the
          epoch form Uwazi expects.
        * Omit the platform-managed common properties (creationDate,
          editDate); they are handled automatically.

    Metadata value shapes (must match exactly):
        * ``text`` / ``markdown`` / ``numeric`` / ``date``: scalar
          (`"hello"`, `42`, `"2024-01-15"`).
        * ``daterange``: ``{"from": "YYYY-MM-DD", "to": "YYYY-MM-DD"}`` or
          the shorthand `"YYYY-MM-DD->YYYY-MM-DD"`.
        * ``multidate``: list of ISO date strings.
        * ``multidaterange``: list of `{"from", "to"}` dicts.
        * ``select``: a thesaurus label string. Never a UUID.
        * ``multiselect``: list of label strings.
        * ``link``: ``{"label": "<text>", "url": "<url>"}`` or
          `"<text>|<url>"`.
        * ``geolocation``: ONE of ``[<lat>, <lon>]``,
          ``{"lat": <lat>, "lon": <lon>}``, or ``"<lat>|<lon>"``. For
          multiple points, wrap any of those in a list. Never build a
          list of objects with ``label``/``lat``/``lon`` keys — that is
          the on-disk envelope and the mapper will reject it.
        * ``image`` / ``media``: URL or file reference.
        * ``relationship``: a list of related entities by their ``shared_id``
          (e.g. ``["k7d2x9ab1cd"]``). Search for the target entities first to
          obtain their ids. On read you receive
          ``[{"shared_id": ..., "title": ...}]``; only ``shared_id`` is used on
          write.

    Args:
        entities: The list of new entities to create.
        language: ISO 639-1 language code. Defaults to "en".

    Returns:
        A per-entity result. On success, ``shared_id`` holds the id Uwazi
        assigned to the newly created entity. On failure, ``shared_id`` is
        empty and ``error`` describes the problem (e.g. unknown template,
        invalid property name, invalid thesaurus label). One failure does
        not abort the rest. On catastrophic error, returns a string with
        suggestions.
    """
    if ctx.deps.entity_api is None:
        return "Error: Entity tools are not configured: `entity_api` is missing on dependencies."
    try:
        return await ctx.deps.entity_api.create_entities(entities=entities, language=language)
    except DomainError as exc:
        template_names = {e.template_name for e in entities if e.template_name}
        if "template" in str(exc).lower() and template_names:
            first_bad = next(iter(template_names))
            return await suggest_template_names(ctx.deps, first_bad)
        return f"Error creating entities: {exc}. Please check the entity data and template names, then retry."
