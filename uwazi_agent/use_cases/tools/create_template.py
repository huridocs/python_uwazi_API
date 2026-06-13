from loguru import logger
from pydantic_ai import RunContext

from uwazi_agent.domain.agent_property import AgentProperty
from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies
from uwazi_api.domain.exceptions import DomainError


async def create_template(
    ctx: RunContext[UwaziAgentToolsDependencies],
    name: str,
    properties: list[AgentProperty],
    color: str = "",
    language: str = "en",
) -> dict | str:
    """Create a new template with the given custom properties.

    Use this when the user wants to define a brand-new template. Provide the
    template's unique name and the list of custom properties it should have.

    The platform-managed common properties (title, creationDate, editDate)
    are added automatically — never include them.

    Each property has a ``name`` and ``type`` plus three Uwazi capability flags
    you can set:
        * ``use_as_filter`` — show the property as a library sidebar filter and
          make it searchable with ``search_entities_by_filter``.
        * ``show_in_card`` — display the property's value on entity summary
          cards in list/search views.
        * ``required`` — forbid saving an entity unless the property has a
          value.

    Linking properties to other data:
        * For ``select``/``multiselect``: set ``thesaurus_name`` to the
          thesaurus to draw values from (created with ``create_thesauri``).
        * For ``relationship``: set ``relationship_type_name`` to an existing
          relationship type (list with ``get_relationship_type_names`` or
          create with ``create_relationship_type``) and, optionally,
          ``related_template_name`` to restrict which template's entities can
          be linked (omit to allow any). The mapper resolves all names to ids.

    Args:
        name: The unique name for the new template.
        properties: The list of custom properties to define on the template.
        color: Optional tint color for the template in the Uwazi UI. Accepts a
            hex string (e.g. ``"#A5915F"``) or a CSS color name (e.g.
            ``"purple"``, ``"steelblue"``); named colors are mapped to their hex
            equivalent. Defaults to no color (``""``).
        language: ISO 639-1 language code. Defaults to "en".

    Returns:
        The API response payload for the created template. On error,
        returns a string describing the problem.
    """
    from uwazi_agent.domain.agent_template import AgentTemplate

    try:
        template = AgentTemplate(name=name, properties=properties, color=color)
        result = await ctx.deps.template_api.create_template(template=template, language=language)
        if isinstance(result, dict):
            refreshed = await ctx.deps.template_api.get_templates_by_names(names=[name])
            if refreshed:
                ctx.deps.schema_store.add_templates(refreshed)
        # Re-fetch the template names list (and counts) so the "Available
        # context" block in the prompt reflects the new template.
        from uwazi_agent.use_cases.tools.tool_context import refresh_templates
        await refresh_templates(ctx)
        return result
    except DomainError as exc:
        logger.error("create_template FAILED: name={} error={}", name, exc)
        return f"Error creating template '{name}': {exc}. Please check the template name and properties, then retry."
