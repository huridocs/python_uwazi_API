from loguru import logger
from pydantic_ai import RunContext

from uwazi_agent.domain.agent_page_menu_link import AgentPageMenuLink
from uwazi_agent.domain.agent_page_menu_link_result import AgentPageMenuLinkResult
from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies
from uwazi_agent.use_cases.tools.page_menu_url import build_page_menu_url, sanitize_menu_title
from uwazi_api.domain.exceptions import DomainError
from uwazi_api.domain.menu_link import MenuLink


async def add_page_menu_links(
    ctx: RunContext[UwaziAgentToolsDependencies],
    links: list[AgentPageMenuLink],
) -> list[AgentPageMenuLinkResult] | str:
    """Register one or more pages in the Uwazi "Settings → Links" menu.

    After ``create_pages`` returns, the page exists but is **not** reachable
    from the public navigation. This tool appends a new entry to the
    instance's existing menu list so the page becomes a clickable link in
    the Uwazi header. The endpoint is a full-replace, so the tool always
    reads the current list, appends your entries, and writes the combined
    list back — any links that were already there are preserved.

    URL hygiene:
        * ``url`` values are stripped of any non-ASCII characters
          (emojis, accents, tabs, stray whitespace). The final URL is
          always ASCII, has no embedded whitespace, and starts with a
          single ``/``.
        * If you pass a ``shared_id``, the URL is built as
          ``/page/{shared_id}`` (or ``/page/{shared_id}/{slug}`` when a
          ``slug`` is given). The ``slug`` is also ASCII-only and
          dash-separated.
        * If you pass neither a ``shared_id`` nor a ``url``, the call
          fails for that entry — pages always live under ``/page/...``.

    Args:
        links: The list of menu entries to add. Each needs a ``title`` and
            either a ``shared_id`` (preferred) or an explicit ``url``.

    Returns:
        One result per requested link, with the final sanitized ``url``
        and a per-entry success flag. One failure does not abort the rest.
        On catastrophic error, returns a string describing the problem.
    """
    if ctx.deps.settings_api is None:
        return "Error: Settings tools are not configured: `settings_api` is missing on dependencies."

    normalized: list[tuple[AgentPageMenuLink, str, str]] = []
    for entry in links:
        title = sanitize_menu_title(entry.title)
        url = build_page_menu_url(entry)
        normalized.append((entry, title, url))

    incomplete = [title for _, title, url in normalized if not title or not url]
    if incomplete:
        return (
            "Error: every menu link must have a non-empty `title` and a "
            "resolvable URL (provide `shared_id` or `url`). "
            f"Invalid entries: {incomplete!r}."
        )

    try:
        existing = await ctx.deps.settings_api.get_menu_links()
    except DomainError as exc:
        logger.error("add_page_menu_links FAILED to read current links: {}", exc)
        return f"Error reading the current menu links: {exc}. Please retry."

    new_entries = [MenuLink(title=title, type="link", url=url) for _, title, url in normalized]

    try:
        await ctx.deps.settings_api.set_menu_links([*existing, *new_entries])
    except DomainError as exc:
        logger.error("add_page_menu_links FAILED to write links: {}", exc)
        return f"Error writing the menu links: {exc}. Please retry."

    return [AgentPageMenuLinkResult(title=title, url=url, success=True) for _, title, url in normalized]
