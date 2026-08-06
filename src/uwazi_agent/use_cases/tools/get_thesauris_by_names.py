from loguru import logger
from pydantic_ai import RunContext

from uwazi_agent.domain.agent_thesauri import AgentThesauri
from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies
from uwazi_api.domain.exceptions import DomainError


async def get_thesauris_by_names(
    ctx: RunContext[UwaziAgentToolsDependencies],
    names: list[str],
    language: str = "en",
) -> list[AgentThesauri] | str:
    """Look up thesauri by their human-readable name, with usage statistics.

    Use this when the user references a thesaurus by name (e.g. "Countries",
    "Languages", "Topics") and you need its current values, the total
    number of times the thesaurus is referenced across entities, and the
    per-value usage breakdown. Names are matched exactly; unknown names are
    silently skipped from the result.

    The stats endpoint is always queried so the returned ``count`` and
    ``value_counts`` reflect the current state of the instance, even if
    the thesaurus contents are served from the in-memory cache.

    Args:
        names: The thesaurus names to look up.
        language: ISO 639-1 language code for the values. Defaults to "en".

    Returns:
        The matching thesauri, each with its current list of value labels,
        a ``count`` (total value references across all entities) and a
        ``value_counts`` dict mapping each value label to its usage count.
        On error, returns a string describing the problem.
    """
    cached = [ctx.deps.schema_store.thesauri[n] for n in names if n in ctx.deps.schema_store.thesauri]
    if len(cached) == len(names):
        thesauri = cached
    else:
        try:
            thesauri = await ctx.deps.thesauri_api.get_thesauris_by_names(names=names, language=language)
            ctx.deps.schema_store.add_thesauri(thesauri)
        except DomainError as exc:
            logger.error("get_thesauris_by_names FAILED: names={} error={}", names, exc)
            return f"Error looking up thesauri: {exc}. Use list_thesauri to see available thesauri and retry."

    if ctx.deps.stats_api is None:
        return thesauri

    try:
        stats = await ctx.deps.stats_api.get_stats(language=language)
    except DomainError as exc:
        logger.error("get_thesauris_by_names FAILED (stats): names={} error={}", names, exc)
        return thesauri

    counts_by_thesaurus: dict[str, dict[str, int]] = {}
    totals_by_thesaurus: dict[str, int] = {}
    for stat in stats.thesauri:
        bucket = counts_by_thesaurus.setdefault(stat.thesaurus_name, {})
        bucket[stat.value_label] = bucket.get(stat.value_label, 0) + stat.count
        totals_by_thesaurus[stat.thesaurus_name] = totals_by_thesaurus.get(stat.thesaurus_name, 0) + stat.count

    enriched: list[AgentThesauri] = []
    for thesaurus in thesauri:
        value_counts = counts_by_thesaurus.get(thesaurus.name, {})
        enriched.append(
            thesaurus.model_copy(
                update={
                    "count": totals_by_thesaurus.get(thesaurus.name, 0),
                    "value_counts": value_counts,
                }
            )
        )
    return enriched
