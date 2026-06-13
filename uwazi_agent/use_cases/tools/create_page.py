"""Unified page-creation tool for the page sub-agent.

Replaces the previous pair of tools (``create_page_from_blocks`` and the
custom-HTML ``create_pages``) with a single tool that handles both
shapes: the LLM either passes a ``blocks`` list (the default
block-template path) or a raw ``content`` body (the custom-HTML/JS
escape hatch). After a successful create, the tool also appends the new
page to the Settings → Links menu so it is reachable from the public
navigation — this is automatic, with no toggle, and the LLM only has to
provide the desired ``menu_title`` for the link.

The block library and available visual themes are NOT fetched from
tools; they are injected into the page agent's system prompt via
``SchemaStore.to_page_prompt_context()`` at boot.
"""

from __future__ import annotations

import hashlib
import json

from loguru import logger
from pydantic_ai import RunContext

from uwazi_agent.domain.agent_page_create import AgentPageCreate
from uwazi_agent.domain.agent_page_mutation_result import AgentPageMutationResult
from uwazi_agent.drivers.page_builder.renderer import DEFAULT_VIBE, PageRenderer
from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies
from uwazi_api.domain.exceptions import DomainError
from uwazi_api.domain.menu_link import MenuLink


def _resolve_vibe(vibe: str | None) -> str:
    """Resolve a possibly-empty user-supplied vibe to a concrete name.

    Centralised here (instead of living in a separate helper module)
    so the page-creation tool is self-contained. Returns the
    ``DEFAULT_VIBE`` (from the renderer module — the single source of
    truth shared with the schema store) when ``vibe`` is ``None`` or
    whitespace; otherwise returns the lower-cased, trimmed input.
    """
    if vibe is None or not vibe.strip():
        return DEFAULT_VIBE
    return vibe.strip().lower()


_MENU_TITLE_DEFAULT_FALLBACK = "Page"


def _canonical_inputs_hash(
    title: str,
    language: str,
    blocks: list[dict] | None,
    content: str | None,
    javascript: str | None,
    vibe: str | None,
    menu_title: str,
) -> str:
    """Stable hash of the user-visible inputs, used for idempotency checks.

    Different pages with the same shape will hash the same; that's
    intentional — the only way to hit a collision is a literal duplicate
    call within the same session, which is the bug this guard prevents.
    """
    payload = {
        "title": title,
        "language": language,
        "blocks": blocks,
        "content": content,
        "javascript": javascript,
        "vibe": vibe,
        "menu_title": menu_title,
    }
    encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


async def _append_menu_link(
    ctx: RunContext[UwaziAgentToolsDependencies],
    title: str,
    shared_id: str,
) -> tuple[bool, str | None]:
    """Append a single Settings → Links entry pointing at ``shared_id``.

    Best-effort: a menu-link failure is logged but does NOT fail the
    create — the page itself was created successfully, and a missing
    menu entry can be added later. Returns ``(success, error_or_none)``.
    """
    if ctx.deps.settings_api is None:
        msg = "Settings API is not configured; the page was created but not added to the menu."
        logger.warning("create_page: {}", msg)
        return False, msg
    try:
        from uwazi_agent.use_cases.tools.page_menu_url import sanitize_menu_title

        clean_title = sanitize_menu_title(title) or _MENU_TITLE_DEFAULT_FALLBACK
        url = f"/page/{shared_id}"
        existing = await ctx.deps.settings_api.get_menu_links()
        new_entry = MenuLink(title=clean_title, type="link", url=url)
        await ctx.deps.settings_api.set_menu_links([*existing, new_entry])
        return True, None
    except DomainError as exc:
        logger.error("create_page: menu link FAILED: {}", exc)
        return False, f"Page created but adding the menu link failed: {exc}."
    except Exception as exc:  # noqa: BLE001 — best-effort, log and move on
        logger.error("create_page: menu link UNEXPECTED FAILURE: {}", exc)
        return False, f"Page created but adding the menu link failed unexpectedly: {exc}."


def _get_page_renderer(
    ctx: RunContext[UwaziAgentToolsDependencies],
) -> PageRenderer | str:
    if ctx.deps.page_builder_dir is None:
        return "Error: Page builder is not configured: `page_builder_dir` is missing on dependencies."
    return PageRenderer(ctx.deps.page_builder_dir)


async def create_page(
    ctx: RunContext[UwaziAgentToolsDependencies],
    title: str,
    language: str = "en",
    blocks: list[dict] | None = None,
    content: str | None = None,
    javascript: str | None = None,
    vibe: str | None = None,
    menu_title: str | None = None,
) -> AgentPageMutationResult | str:
    """Create a new Settings → Page in Uwazi and add it to the public menu.

    This is the default way to create pages. Pick the path by which
    argument you pass:

    * ``blocks=[...]`` — the default block-template path. Composes
      pre-styled blocks (hero, content, stats_grid, card_grid, timeline,
      two_column, pie_chart, bar_chart, cta, divider, plus the two
      event-chart variants) under a named visual theme (``vibe``). The
      full block library and available vibes are already in your system
      context — you do NOT need to call any tool to learn them.
    * ``content=...`` — the custom-HTML/JS escape hatch. Use this only
      when the user explicitly asks for a layout that the block
      library cannot express (e.g. a bespoke layout, an embedded
      third-party widget, custom interactive code). The content is
      rendered as Markdown; raw HTML inside it is allowed. You can
      additionally pass ``javascript=...`` to populate the page's
      "Javascript" tab.

    You must pass exactly one of ``blocks`` or ``content`` — passing
    both, or neither, returns an ``Error:`` string.

    Args:
        title: The page's title (also the Settings → Pages entry label
            and the default menu button text if ``menu_title`` is not
            provided).
        language: ISO 639-1 language code (e.g. ``en``, ``fr``, ``es``).
            Defaults to ``en``.
        blocks: An ordered list of block definitions for the
            block-template path. Each block has a ``type`` (see the
            block library in your system context) and a ``slots``
            dict whose shape matches that block's slot schema.
        content: The full Markdown/HTML body of the page. Used for
            the custom-HTML/JS path; mutually exclusive with
            ``blocks``.
        javascript: Optional JavaScript for the page's "Javascript"
            tab. Only meaningful with ``content=``.
        vibe: Optional visual-theme name (e.g. ``minimal``,
            ``corporate``, ``activist``, ``earth``, ``ocean``,
            ``warm``). Only meaningful with ``blocks=``. If omitted or
            empty, the default vibe (``minimal``) is used.
        menu_title: The label that will appear in the Settings → Links
            menu for this page. If you do not pass one, the page's
            ``title`` is used. Do NOT add emojis, tabs, or accented
            characters — the tool sanitises the title for you.

    Returns:
        On success, an :class:`AgentPageMutationResult` with the new
        ``shared_id`` and public ``url``. On failure (validation,
        rendering, or Uwazi rejection), returns a string beginning with
        ``Error:`` describing the problem; the LLM should read the
        error, fix the inputs, and retry.
    """
    if ctx.deps.page_api is None:
        return "Error: Page tools are not configured: `page_api` is missing on dependencies."

    has_blocks = bool(blocks)
    has_content = bool(content)
    if has_blocks and has_content:
        return (
            "Error: pass either `blocks` (the default block-template path) "
            "or `content` (the custom-HTML path), not both."
        )
    if not has_blocks and not has_content:
        return (
            "Error: pass one of `blocks` (the default block-template path) "
            "or `content` (the custom-HTML path)."
        )

    # Idempotency check: same inputs in the same session -> already created.
    effective_menu_title = (menu_title or title or _MENU_TITLE_DEFAULT_FALLBACK).strip()
    inputs_hash = _canonical_inputs_hash(
        title=title,
        language=language,
        blocks=blocks,
        content=content,
        javascript=javascript,
        vibe=vibe,
        menu_title=effective_menu_title,
    )
    prior = ctx.deps.tool_cache.get("create_page", {"_hash": inputs_hash})
    if prior is not None:
        return (
            f"Error: a page with these exact inputs was already created in this session "
            f"(shared_id={prior.get('shared_id', '?')}). To make a different page, change the "
            f"title or content; to update this one, call `update_pages` instead."
        )

    # Resolve the final body.
    if has_blocks:
        renderer_or_err = _get_page_renderer(ctx)
        if isinstance(renderer_or_err, str):
            return renderer_or_err
        resolved_vibe = _resolve_vibe(vibe)
        try:
            html_body = renderer_or_err.render(vibe=resolved_vibe, blocks=blocks or [])
        except ValueError as exc:
            return (
                f"Error rendering page: {exc}. "
                "Check block slot values against the schemas in your system context."
            )
        except Exception as exc:  # noqa: BLE001 — propagate with guidance
            return (
                f"Error rendering page: {exc}. "
                "Check block slot values against the schemas in your system context."
            )
        page = AgentPageCreate(
            title=title,
            content=html_body,
            language=language,
            entity_view=False,
        )
    else:
        page = AgentPageCreate(
            title=title,
            content=content or "",
            javascript=javascript,
            language=language,
            entity_view=False,
        )

    try:
        results = await ctx.deps.page_api.create_pages(pages=[page], language=language)
    except DomainError as exc:
        logger.error("create_page FAILED: {}", exc)
        return f"Error creating page: {exc}. The page was not created in Uwazi."

    if not results:
        return "Error creating page: Uwazi returned no result for the create request."

    result = results[0]
    if not result.success or not result.shared_id:
        return (
            f"Error creating page: {result.error or 'unknown failure'}. "
            "Fix the inputs and retry."
        )

    # Cache the successful create BEFORE the menu-link step so an
    # idempotency retry sees the same shared_id even if the menu link
    # step is in flight.
    ctx.deps.tool_cache.set(
        "create_page",
        {"_hash": inputs_hash},
        {"shared_id": result.shared_id, "url": result.url},
    )

    # Best-effort menu-link append.
    menu_ok, menu_err = await _append_menu_link(ctx, effective_menu_title, result.shared_id)
    if not menu_ok:
        # Page is created; surface the menu issue but don't fail the
        # whole result. The LLM should still report the page as created.
        logger.warning(
            "create_page: page {} created but menu link step failed: {}",
            result.shared_id,
            menu_err,
        )

    return result
