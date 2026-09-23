"""Authoring-time tool for external URL fetching (source-page HTML sampling).

``peek_file_text`` only reaches files *stored on Uwazi* (``get_file_bytes`` by
storage filename), so an entity whose extraction values live on a referenced
page — its ``Source Page URL`` (a ``link`` metadata property) or a URL
attachment — is invisible to the generation agent when it has no uploaded
supporting file. ``peek_url_text`` gives the agent that window: fetch an
``http(s)`` URL's body as text, truncated to ``URL_FETCH_MAX_CHARS``.

This is the AUTHORING-time tool (``peek_``-prefixed, like ``peek_file_text``),
distinct from the exec-sandbox ``get_url_text`` helper the generated SCRIPT
calls at execute time — which returns the full (byte-capped) text to the pure
``extract`` function. Both ride the :class:`UrlFetcherPort` wired on
:class:`AdminAgentDeps`; a missing port or a failed fetch degrades to an error
STRING (never raises — a tool error is LLM-visible context, not a crash).
"""

from __future__ import annotations

from pydantic_ai import RunContext

from uwazi_admin_agent.configuration import URL_FETCH_MAX_CHARS
from uwazi_admin_agent.domain.url_fetch import truncate_url_text
from uwazi_admin_agent.ports.url_fetcher_port import UrlFetchError
from uwazi_admin_agent.use_cases.admin_agent_deps import AdminAgentDeps


async def peek_url_text(ctx: RunContext[AdminAgentDeps], url: str) -> str:
    """Fetch an external ``http(s)`` URL (e.g. an entity's Source Page URL) and
    return its body decoded as text, truncated head+tail to ``URL_FETCH_MAX_CHARS``.
    Returns an error string on failure (never raises)."""
    deps: AdminAgentDeps = ctx.deps
    if deps.url_fetcher is None:
        return "Error: url_fetcher is not wired; cannot fetch external URLs."
    if not url:
        return "Error: url must be a non-empty http(s) URL."
    try:
        text = await deps.url_fetcher.fetch_text(url)
    except UrlFetchError as exc:
        return f"Error: {exc}"
    except Exception as exc:  # noqa: BLE001 - degrade to an error string, never crash the agent turn
        return f"Error: fetch failed for {url}: {type(exc).__name__}: {exc}"
    return truncate_url_text(text, URL_FETCH_MAX_CHARS)
