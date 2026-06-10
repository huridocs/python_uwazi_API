from pydantic_ai import RunContext

from uwazi_agent.domain.agent_entity import AgentEntity
from uwazi_agent.domain.agent_entity_mutation_result import AgentEntityMutationResult
from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies
from uwazi_agent.use_cases.tools.fail_forward import suggest_template_names
from uwazi_api.domain.exceptions import DomainError


async def update_entities(
    ctx: RunContext[UwaziAgentToolsDependencies],
    updates: list[AgentEntity],
    language: str = "en",
) -> list[AgentEntityMutationResult] | str:
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

    Metadata value shapes (must match exactly):
        * ``text`` / ``markdown`` / ``numeric`` / ``date``: scalar
          (`"hello"`, `42`, `"2024-01-15"`).
        * ``daterange``: ``{"from": "YYYY-MM-DD", "to": "YYYY-MM-DD"}`` or
          the shorthand `"YYYY-MM-DD->YYYY-MM-DD"`.
        * ``multidate``: list of ISO date strings.
        * ``multidaterange``: list of `{"from", "to"}` dicts.
        * ``select``: a thesaurus label string (e.g. `"Approved"`). Never
          a UUID.
        * ``multiselect``: list of label strings.
        * ``link``: ``{"label": "<text>", "url": "<url>"}`` or
          `"<text>|<url>"`.
        * ``geolocation``: ONE of ``[<lat>, <lon>]``,
          ``{"lat": <lat>, "lon": <lon>}``, or ``"<lat>|<lon>"``. For
          multiple points, wrap any of those in a list. Never build a
          list of objects with ``label``/``lat``/``lon`` keys — that is
          the on-disk envelope and the mapper will reject it.
        * ``image`` / ``media``: URL or file reference.

    Round-tripping: entities returned by ``get_entities_by_shared_ids``,
    ``search_entities_by_text``, and ``get_entities_by_template`` are
    already in these shapes. When you copy a metadata value into an
    update payload, copy it verbatim — including ``[lat, lon]`` pairs
    for geolocation.

    Args:
        updates: The list of partial entity updates to apply.
        language: ISO 639-1 language code. Defaults to "en".

    Returns:
        A per-entity result indicating success or a descriptive error
        (e.g. unknown shared_id, unknown template, invalid thesaurus
        label). Failures in one entity do not abort the rest. On
        catastrophic error, returns a string with suggestions.
    """
    if ctx.deps.entity_api is None:
        return "Error: Entity tools are not configured: `entity_api` is missing on dependencies."
    try:
        return await ctx.deps.entity_api.update_entities(updates=updates, language=language)
    except DomainError as exc:
        template_names = {e.template_name for e in updates if e.template_name}
        if "template" in str(exc).lower() and template_names:
            first_bad = next(iter(template_names))
            return await suggest_template_names(ctx.deps, first_bad)
        return f"Error updating entities: {exc}. Please check the entity data and template names, then retry."
