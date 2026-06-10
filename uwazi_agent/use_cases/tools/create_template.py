from loguru import logger
from pydantic_ai import RunContext

from uwazi_agent.domain.agent_property import AgentProperty
from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies
from uwazi_api.domain.exceptions import DomainError


async def create_template(
    ctx: RunContext[UwaziAgentToolsDependencies],
    name: str,
    properties: list[AgentProperty],
    language: str = "en",
) -> dict | str:
    logger.info("create_template(name={!r}, properties_count={}, language={!r})", name, len(properties), language)
    """Create a new template with the given custom properties.

    Use this when the user wants to define a brand-new template. Provide the
    template's unique name and the list of custom properties it should have.

    The platform-managed common properties (title, creationDate, editDate)
    are added automatically — never include them.

    For properties of type ``select`` or ``multiselect``, set ``thesaurus_name``
    to the name of the thesaurus to link to. The mapper will resolve it to the
    Uwazi id under the hood. Property types of ``relationship`` are not
    supported yet (TODO).

    Args:
        name: The unique name for the new template.
        properties: The list of custom properties to define on the template.
        language: ISO 639-1 language code. Defaults to "en".

    Returns:
        The API response payload for the created template. On error,
        returns a string describing the problem.
    """
    from uwazi_agent.domain.agent_template import AgentTemplate

    try:
        template = AgentTemplate(name=name, properties=properties)
        return await ctx.deps.template_api.create_template(template=template, language=language)
    except DomainError as exc:
        return f"Error creating template '{name}': {exc}. Please check the template name and properties, then retry."
