from loguru import logger
from pydantic_ai import RunContext

from uwazi_agent.domain.agent_property import AgentProperty
from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies
from uwazi_agent.use_cases.tools.fail_forward import suggest_template_names
from uwazi_api.domain.exceptions import DomainError


async def update_template(
    ctx: RunContext[UwaziAgentToolsDependencies],
    name: str,
    properties: list[AgentProperty],
    color: str = "",
    language: str = "en",
) -> str:
    """Replace the custom properties of an existing template.

    Use this when the user wants to change the set of custom properties on
    a template. The provided list **replaces** the existing custom property
    list; pass the full desired set, not a diff. To preserve a property,
    re-send it (look it up first with ``get_templates_by_names``); to keep its
    capability flags, re-send those too.

    The platform-managed common properties (title, creationDate, editDate)
    are always preserved and are not part of the input.

    Each property supports the same fields as in ``create_template``:
        * ``use_as_filter`` — show as a sidebar filter and make filterable.
        * ``show_in_card`` — show on entity summary cards.
        * ``required`` — require a value before an entity can be saved.
        * ``thesaurus_name`` — for ``select``/``multiselect``, the thesaurus
          to link to.
        * ``relationship_type_name`` (and optional ``related_template_name``)
          — for ``relationship`` properties. The mapper resolves names to ids.

    Args:
        name: The template name to update.
        properties: The new full list of custom properties.
        color: Optional tint color for the template in the Uwazi UI. Accepts a
            hex string (e.g. ``"#A5915F"``) or a CSS color name (e.g.
            ``"purple"``, ``"steelblue"``); named colors are mapped to their hex
            equivalent. If omitted, the template's existing color is preserved.
        language: ISO 639-1 language code. Defaults to "en".

    Returns:
        A confirmation string on success. On error, returns a
        string with suggestions.
    """
    from uwazi_agent.domain.agent_template import AgentTemplate

    try:
        resolved_color = color
        if not resolved_color:
            existing = ctx.deps.schema_store.templates.get(name)
            if existing is not None:
                resolved_color = existing.color
        template = AgentTemplate(name=name, properties=properties, color=resolved_color)
        await ctx.deps.template_api.update_template(template=template, language=language)
        property_names = [p.name for p in properties]
        # Re-fetch the cached template entry so the "Available context"
        # block in the prompt reflects the updated template.
        from uwazi_agent.use_cases.tools.tool_context import refresh_templates
        await refresh_templates(ctx)
        return (
            f"Template '{name}' updated successfully. "
            f"It now has {len(properties)} custom properties: {', '.join(property_names)}."
        )
    except DomainError as exc:
        logger.error("update_template FAILED: name={} error={}", name, exc)
        if "not found" in str(exc).lower():
            return await suggest_template_names(ctx.deps, name)
        return f"Error updating template '{name}': {exc}. Please check the template name and properties, then retry."
