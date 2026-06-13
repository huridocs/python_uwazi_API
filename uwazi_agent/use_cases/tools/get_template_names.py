from loguru import logger
from pydantic_ai import RunContext

from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies
from uwazi_api.domain.exceptions import DomainError


async def list_templates(
    ctx: RunContext[UwaziAgentToolsDependencies],
) -> list[dict] | str:
    """List all templates available in the Uwazi instance, with the number of
    entities that use each one.

    Use this to discover what templates exist — and how heavily each is used —
    before the user asks to read, create, update or delete one. The entity
    count is useful for understanding data distribution and for choosing
    which template to focus on.

    Returns:
        A list of dicts, one per template, each with:
        - ``name`` (str): the template's human-readable name.
        - ``count`` (int): number of entities using that template, sorted
          by count descending. On error, returns a string describing the
          problem.
    """
    names: list[str] = []
    if not ctx.deps.schema_store.template_names:
        try:
            names = await ctx.deps.template_api.get_template_names()
            ctx.deps.schema_store.add_template_names(names)
        except DomainError as exc:
            logger.error("list_templates FAILED (names): {}", exc)
            return f"Error listing template names: {exc}. Please check the Uwazi connection and retry."
    else:
        names = ctx.deps.schema_store.template_names

    if ctx.deps.stats_api is None:
        return [{"name": n, "count": 0} for n in names]

    try:
        stats = await ctx.deps.stats_api.get_stats()
    except DomainError as exc:
        logger.error("list_templates FAILED (stats): {}", exc)
        return [{"name": n, "count": 0} for n in names]

    count_by_name = {t.template_name: t.count for t in stats.templates}
    return sorted(
        ({"name": n, "count": count_by_name.get(n, 0)} for n in names),
        key=lambda entry: -entry["count"],
    )
