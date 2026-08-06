from loguru import logger
from pydantic_ai import RunContext

from uwazi_agent.configuration import ENTITIES_LIMIT_FOR_LLM_MODEL
from uwazi_agent.domain.agent_entity_search_result import AgentEntitySearchResult
from uwazi_agent.domain.agent_search_filter import AgentSearchFilter
from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies
from uwazi_agent.use_cases.tools.fail_forward import suggest_template_names
from uwazi_api.domain.exceptions import DomainError, PropertyNotFilterableError


async def search_entities_by_filter(
    ctx: RunContext[UwaziAgentToolsDependencies],
    template_name: str,
    filters: list[AgentSearchFilter],
    language: str = "en",
    limit: int = 10000,
    published: bool | None = None,
) -> AgentEntitySearchResult | str:
    """Find entities of a template by exact-match filters on its properties.

    Use this for structured queries — "all Films from Japan", "Refugees who
    crossed between two dates" — where ``search_entities_by_text`` (fuzzy
    free-text) is too imprecise. Filters match on the property's *value*, not
    on free text.

    How filters work in Uwazi:
        * Only properties marked ``use_as_filter`` on the template can be
          filtered. Inspect the template first with ``get_templates_by_names``
          and confirm ``use_as_filter`` is true and learn the exact
          ``property_name`` to use.
        * For ``select``/``multiselect`` properties: set ``values`` to a list
          of thesaurus **labels** (never UUIDs). An entity matches if it has
          ANY of the listed values. Multiple filters are combined with AND.
        * For ``date``/``daterange`` properties: set ``date_from`` and/or
          ``date_to`` as ISO ``YYYY-MM-DD`` bounds (inclusive).
        * One ``AgentSearchFilter`` describes one property. Pass several to
          require all of them at once.

    The result has the same summary + examples shape as the other entity
    search tools, and all matches are stored in the session entity store for
    batch processing.

    Args:
        template_name: The template to search within. Pass the *name*, not its
            id. A template is required so filter properties can be validated
            and thesaurus labels resolved.
        filters: The list of filter conditions to apply (combined with AND).
        language: ISO 639-1 language code. Match the language the user is
            working in; defaults to "en".
        limit: Maximum number of entities to fetch. Defaults to 10000.
        published: If true, only published entities; if false, only
            unpublished; if omitted, both.

    Returns:
        A search result with a ``summary`` and a few ``examples``. On error
        (e.g. a non-filterable property or an invalid thesaurus label),
        returns a string explaining how to correct the call.
    """
    if ctx.deps.entity_api is None:
        return "Error: Entity tools are not configured: `entity_api` is missing on dependencies."
    try:
        result = await ctx.deps.entity_api.search_entities_by_filter(
            template_name=template_name,
            filters=filters,
            language=language,
            limit=limit,
            published=published,
        )
    except PropertyNotFilterableError as exc:
        logger.error(
            "search_entities_by_filter REJECTED: template={} property={} filterable={}",
            template_name,
            exc.property_name,
            exc.filterable_properties,
        )
        return (
            f"Error: property '{exc.property_name}' is not filterable on template "
            f"'{exc.template_name}'. Only properties with `use_as_filter` set on the template "
            f"can be passed to search_entities_by_filter. Filterable properties on this "
            f"template: {exc.filterable_properties}. Call get_templates_by_names to inspect "
            f"the template's properties and their `use_as_filter` flag, then retry with a "
            f"filterable property."
        )
    except DomainError as exc:
        logger.error("search_entities_by_filter FAILED: template={} filters={} error={}", template_name, filters, exc)
        if "not found" in str(exc).lower() and "template" in str(exc).lower():
            return await suggest_template_names(ctx.deps, template_name)
        return (
            f"Error filtering entities: {exc}. Confirm the property is marked use_as_filter "
            "on the template and that select values are valid thesaurus labels, then retry."
        )
    all_entities = result._all_entities
    if all_entities:
        ctx.deps.entity_store.add_entities(all_entities)
    if result.summary.count > ENTITIES_LIMIT_FOR_LLM_MODEL:
        ctx.deps.entity_store.needs_python_agent = True
        result.summary.note = (
            f"{result.summary.count} entities found and stored in the entity store. "
            f"This exceeds the LLM limit of {ENTITIES_LIMIT_FOR_LLM_MODEL}. "
            f"You MUST delegate to the Python agent for batch processing."
        )
    return result
