"""Tool for removing entries from the Uwazi "Settings → Links" menu.

The Uwazi links endpoint is a full-replace store, so deletions are expressed
by matching against the current list and writing back everything that did
NOT match.
"""

from __future__ import annotations

from loguru import logger
from pydantic_ai import RunContext

from uwazi_agent.domain.agent_page_menu_link_result import AgentPageMenuLinkResult
from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies
from uwazi_api.domain.exceptions import DomainError
from uwazi_api.domain.menu_link import MenuLink


def _url_points_at_page(url: str | None, shared_id: str) -> bool:
    """Return True when a menu link URL targets the given page shared_id.

    Uwazi page URLs are ``/page/{shared_id}`` or ``/page/{shared_id}/{slug}``.
    The comparison is case-sensitive and only matches the path component.
    """
    if not url:
        return False
    path = url.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    return path == f"/page/{shared_id}" or path.startswith(f"/page/{shared_id}/")


async def delete_page_menu_links(
    ctx: RunContext[UwaziAgentToolsDependencies],
    shared_ids: list[str],
) -> list[AgentPageMenuLinkResult] | str:
    """Remove Settings → Links entries that point at the given page(s).

    Uwazi's links endpoint stores the complete public navigation list, so this
    tool reads the current list, drops every entry whose URL targets one of
    the supplied ``shared_ids``, and writes the remaining entries back. Other
    links (entity views, external URLs, pages with a different shared_id)
    are preserved.

    Args:
        shared_ids: The Uwazi page shared_ids whose menu entries should be
            removed.

    Returns:
        One result per deleted link indicating the title and URL that was
        removed. If no matching links exist, returns an empty list. On
        catastrophic error, returns a string describing the problem.
    """
    if not shared_ids:
        return []

    if ctx.deps.settings_api is None:
        return "Error: Settings tools are not configured: `settings_api` is missing on dependencies."

    try:
        existing = await ctx.deps.settings_api.get_menu_links()
    except DomainError as exc:
        logger.error("delete_page_menu_links FAILED to read current links: {}", exc)
        return f"Error reading the current menu links: {exc}. Please retry."

    target_ids = set(shared_ids)
    removed: list[MenuLink] = []
    kept: list[MenuLink] = []
    for link in existing:
        if link.url and any(_url_points_at_page(link.url, sid) for sid in target_ids):
            removed.append(link)
        else:
            kept.append(link)

    if not removed:
        return []

    try:
        await ctx.deps.settings_api.set_menu_links(kept)
    except DomainError as exc:
        logger.error("delete_page_menu_links FAILED to write links: {}", exc)
        return f"Error writing the menu links: {exc}. Please retry."

    return [
        AgentPageMenuLinkResult(
            title=link.title,
            url=link.url or "",
            success=True,
        )
        for link in removed
    ]
