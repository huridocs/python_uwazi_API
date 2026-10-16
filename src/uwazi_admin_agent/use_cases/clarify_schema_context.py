"""Instance-schema context for the prompt-clarification agent (option 1).

The clarification agent is TOOL-FREE by design (one cheap LLM call), so it
cannot call ``list_templates`` / ``get_templates_by_names`` the way the
generation agent can — which is exactly why it used to ask clarifying
questions ("which property?", "which template?") that the instance's own
schema answers. This module fetches the instance's template + thesaurus
definitions through the ALREADY-WIRED :class:`AdminAgentDeps` ports and
renders them as compact plain text the clarification system prompt embeds.

Renders ALL templates (names + every custom property with its type, filter
flag, and thesaurus name) plus the thesaurus NAMES — the level of detail a
prompt-reader needs to know which property names are real. Thesaurus VALUES
are deliberately NOT fetched (they can be hundreds of labels per thesaurus;
the generation agent resolves exact labels itself with
``get_thesauris_by_names``).

Pure formatting over fetched models; the fetch is best-effort: a failure to
read the schema must never block the clarification step (the agent falls back
to asking, behaving exactly as before).
"""

from __future__ import annotations

import asyncio

from loguru import logger

from uwazi_admin_agent.use_cases.admin_agent_deps import AdminAgentDeps
from uwazi_agent.domain.agent_template import AgentTemplate


def render_template(template: AgentTemplate) -> str:
    """Render one template's property list as one line (pure)."""
    if not template.properties:
        return f"- {template.name}: (no custom properties)"
    parts: list[str] = []
    for prop in template.properties:
        # AgentPropertyType is a str-Enum; .value gives the lowercase Uwazi
        # name ("select", "multiselect", ...) the property docs use.
        descriptor = prop.type.value if hasattr(prop.type, "value") else str(prop.type)
        if prop.thesaurus_name:
            descriptor += f"; thesaurus: {prop.thesaurus_name}"
        rendered = f"{prop.name} ({descriptor})"
        if prop.use_as_filter:
            rendered += " [filterable]"
        parts.append(rendered)
    return f"- {template.name}: {'; '.join(parts)}"


def render_schema(templates: list[AgentTemplate], thesaurus_names: list[str]) -> str:
    """Render the fetched schema as the block the clarification prompt embeds (pure).

    Section headers are the literal strings the system prompt references.
    """
    template_lines = [render_template(t) for t in templates] or ["- (none)"]
    thesaurus_lines = [f"- {name}" for name in thesaurus_names] or ["- (none)"]
    return "\n".join(
        [
            "INSTANCE TEMPLATES (real property names — never ask which property exists; use these):",
            *template_lines,
            "",
            "THESAURI NAMES (controlled vocabularies for select/multiselect values):",
            *thesaurus_lines,
        ]
    )


async def fetch_schema_context(deps: AdminAgentDeps | None) -> str:
    """Fetch the instance schema through the wired deps and render it (best-effort).

    Templates and thesaurus names are fetched in parallel (two independent
    read calls). Any failure — unwired ports, a dead instance, a domain error —
    degrades to a note naming the gap instead of raising: clarification must
    stay available when generation-time schema reads work but this pass's
    fetch hits a transient problem.
    """
    if deps is None:
        return "(instance schema unavailable: no template/thesaurus API wired — ask about anything the schema would answer)"
    try:
        templates, thesauri = await asyncio.gather(
            deps.template_api.get_templates(),
            deps.thesauri_api.get_thesauris(language="en"),
        )
        thesaurus_names = [t.name for t in thesauri]
    except Exception as exc:  # noqa: BLE001 — degrade, never block clarification
        logger.warning("clarification schema fetch degraded: {}", exc)
        return (
            f"(instance schema unavailable: fetch failed ({type(exc).__name__}) — "
            "ask about anything the schema would answer)"
        )
    return render_schema(list(templates), thesaurus_names)
