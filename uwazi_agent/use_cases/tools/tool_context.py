"""Pre-loading and refreshing the "available context" data sets.

The orchestrator no longer exposes ``get_languages``, ``list_templates``,
``list_thesauri`` or ``get_relationship_type_names`` as tools. Instead, the
four data sets are pre-loaded once at run start and re-fetched on demand
whenever a write tool mutates the underlying data. The result is rendered
into the user prompt via :meth:`SchemaStore.to_available_context`.

The functions in this module are the single source of truth for that
loading/refreshing logic. They are designed to fail soft: a transient Uwazi
error never aborts the agent run -- the relevant section is just omitted
from the prompt and a warning is logged.
"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger
from pydantic_ai import RunContext

from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies
from uwazi_agent.use_cases.tools.schema_store import TemplateEntry
from uwazi_api.domain.exceptions import DomainError


async def _safe(coro: Any, *, label: str, default: Any) -> Any:
    """Await ``coro``; on ``DomainError`` or any other exception, log a
    warning and return ``default``. Used so a transient Uwazi hiccup never
    crashes the run -- the prompt just omits the affected section."""
    try:
        return await coro
    except DomainError as exc:
        logger.warning("available_context: {} FAILED (DomainError): {}", label, exc)
        return default
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 -- defensive: never let pre-load break the run
        logger.warning("available_context: {} FAILED: {}", label, exc)
        return default


async def load_languages(ctx: RunContext[UwaziAgentToolsDependencies]) -> None:
    """Populate ``schema_store.languages`` with the configured languages."""
    if ctx.deps.settings_api is None:
        return
    languages = await _safe(
        ctx.deps.settings_api.get_languages(),
        label="load_languages",
        default=[],
    )
    if languages:
        ctx.deps.schema_store.set_languages(languages)


async def load_templates(ctx: RunContext[UwaziAgentToolsDependencies]) -> None:
    """Populate ``schema_store.template_entries`` (name + entity count).

    Two HTTP calls:
    1. ``template_api.get_template_names()`` for the names.
    2. ``stats_api.get_stats()`` (when available) to attach per-template
       entity counts.

    Failures on the stats endpoint are non-fatal; we just leave the counts
    at zero.
    """
    names = await _safe(
        ctx.deps.template_api.get_template_names(),
        label="load_templates (names)",
        default=[],
    )
    if not names:
        return
    count_by_name: dict[str, int] = {}
    if ctx.deps.stats_api is not None:
        stats = await _safe(
            ctx.deps.stats_api.get_stats(),
            label="load_templates (stats)",
            default=None,
        )
        if stats is not None:
            count_by_name = {t.template_name: t.count for t in stats.templates}
    entries = [
        TemplateEntry(name=name, count=count_by_name.get(name, 0)) for name in names
    ]
    # ``set_template_entries`` preserves any names already known to the store
    # (e.g. ones added by ``create_template`` between loads) so we never lose
    # a name on refresh.
    ctx.deps.schema_store.set_template_entries(entries)
    # Also keep ``template_names`` in sync for the tools that still read it.
    ctx.deps.schema_store.add_template_names(names)


async def load_thesauri_names(ctx: RunContext[UwaziAgentToolsDependencies]) -> None:
    """Populate ``schema_store.thesauri_names`` with the names of all
    thesauri. Uses the default language ``"en"`` for the labels because the
    snapshot is only used for the human-readable list in the prompt; tools
    that need the full values re-fetch by name with the right language.
    """
    language = "en"
    thesauris = await _safe(
        ctx.deps.thesauri_api.get_thesauris(language=language),
        label="load_thesauri_names",
        default=[],
    )
    if not thesauris:
        return
    names = [t.name for t in thesauris]
    ctx.deps.schema_store.add_thesauri_names(names)


async def load_relationship_type_names(
    ctx: RunContext[UwaziAgentToolsDependencies],
) -> None:
    """Populate ``schema_store.relationship_type_names``."""
    if ctx.deps.relationship_type_api is None:
        return
    names = await _safe(
        ctx.deps.relationship_type_api.get_relationship_type_names(),
        label="load_relationship_type_names",
        default=[],
    )
    if names:
        ctx.deps.schema_store.set_relationship_type_names(names)


async def populate_all(ctx: RunContext[UwaziAgentToolsDependencies]) -> None:
    """Pre-load all four data sets. Called once at run start.

    Each individual loader is fault-isolated; the others still run if one
    fails. Run them concurrently to keep the run-start cost bounded by the
    slowest single call rather than their sum.
    """
    await asyncio.gather(
        load_languages(ctx),
        load_templates(ctx),
        load_thesauri_names(ctx),
        load_relationship_type_names(ctx),
    )


# ---------------------------------------------------------------------------
# Re-fetch helpers used by write tools after a mutation.
#
# These are intentionally narrow: a write tool calls exactly the re-fetch
# that matches the data it just changed, and only when the relevant port is
# configured. This keeps the per-mutation overhead to a single extra HTTP
# call in the common case.
# ---------------------------------------------------------------------------


async def refresh_languages(ctx: RunContext[UwaziAgentToolsDependencies]) -> None:
    """Re-fetch languages. Languages are a Uwazi instance setting, so the
    only write tool that triggers this is the (not-yet-exposed) settings
    tool, but we keep the helper symmetric for future use."""
    await load_languages(ctx)


async def refresh_templates(ctx: RunContext[UwaziAgentToolsDependencies]) -> None:
    """Re-fetch template names (and counts). Called by ``create_template``,
    ``update_template`` and ``delete_template``."""
    await load_templates(ctx)


async def refresh_thesauri_names(
    ctx: RunContext[UwaziAgentToolsDependencies],
) -> None:
    """Re-fetch thesaurus names. Called by ``create_thesauri``,
    ``update_thesauri`` and ``delete_thesauri``."""
    await load_thesauri_names(ctx)


async def refresh_relationship_type_names(
    ctx: RunContext[UwaziAgentToolsDependencies],
) -> None:
    """Re-fetch relationship type names. Called by
    ``create_relationship_type``, ``update_relationship_type`` and
    ``delete_relationship_type``."""
    await load_relationship_type_names(ctx)
