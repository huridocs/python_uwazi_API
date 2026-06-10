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
    language: str = "en",
) -> dict | str:
    logger.info("update_template(name={!r}, properties_count={}, language={!r})", name, len(properties), language)
    """Replace the custom properties of an existing template.

    Use this when the user wants to change the set of custom properties on
    a template. The provided list **replaces** the existing custom property
    list; pass the full desired set, not a diff.

    The platform-managed common properties (title, creationDate, editDate)
    are always preserved and are not part of the input.

    For properties of type ``select`` or ``multiselect``, set ``thesaurus_name``
    to the name of the thesaurus to link to. Property types of ``relationship``
    are not supported yet (TODO).

    Args:
        name: The template name to update.
        properties: The new full list of custom properties.
        language: ISO 639-1 language code. Defaults to "en".

    Returns:
        The API response payload for the update. On error, returns a
        string with suggestions.
    """
    from uwazi_agent.domain.agent_template import AgentTemplate

    try:
        template = AgentTemplate(name=name, properties=properties)
        return await ctx.deps.template_api.update_template(template=template, language=language)
    except DomainError as exc:
        if "not found" in str(exc).lower():
            return await suggest_template_names(ctx.deps, name)
        return f"Error updating template '{name}': {exc}. Please check the template name and properties, then retry."
