import asyncio
import collections
import itertools
import json
import math
import re
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic_ai import RunContext

from uwazi_agent import configuration
from uwazi_agent.domain.agent_page_create import AgentPageCreate
from uwazi_agent.drivers.page_builder.renderer import PageRenderer
from uwazi_agent.ports.page_api_port import PageApiPort
from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies
from uwazi_agent.use_cases.tools.entity_store import EntityStore
from uwazi_agent.use_cases.tools.render_page_from_blocks import _resolve_vibe

_PENDING_SCRIPT_KEY: str = "__pending_page_script__"


class RenderedPage:
    """The result of ``render_blocks`` — body and CSS in separate strings.

    Pass ``.body`` as the page's HTML and ``.css`` as its stylesheet; do
    NOT concatenate them yourself (inlining ``<style>`` tags inside the
    body triggers a React 18 hydration error in the public page).
    """

    __slots__ = ("body", "css")

    def __init__(self, body: str, css: str) -> None:
        self.body = body
        self.css = css

    def __repr__(self) -> str:
        return f"RenderedPage(body_len={len(self.body)}, css_len={len(self.css)})"


async def prepare_page_script(
    ctx: RunContext[UwaziAgentToolsDependencies],
    code: str,
) -> str:
    """Write (or overwrite) a Python script that builds a page programmatically.

    The script is NOT executed yet — it is stored in the session entity store.
    Call ``execute_page_script`` to run it and create the page in Uwazi.

    This is the **primary** way to create data-driven pages (timelines,
    catalogs, dashboards, card grids, stats grids, charts) when the data
    comes from Uwazi entities already loaded in the session entity store
    or from data payloads prepared by the Python agent.

    **Why use scripts instead of inline block composition?**

    The old ``create_page_from_blocks`` tool required you (the LLM) to pass
    every block slot value inline — every heading, every card title, every
    timeline entry. When a page is driven by hundreds or thousands of
    entities this is impossible: the data would not fit in a single tool
    call. A page script solves this by giving you programmatic access to
    the entity store and data payloads at **execution time**, not at
    prompt time. You describe *what the code should do* (not the data
    itself) and the code reads the data from the store when it runs.

    **Workflow:**

    1. ``list_page_blocks`` to learn available block types and their
       slot schemas.
    2. ``list_page_vibes`` to see available visual themes (or omit ``vibe``
       to get the ``minimal`` default).
    3. (Optional) ``list_entity_store_data_payload_keys`` to see what
       prepared data is available.
    4. (Optional) ``get_entity_store_data_payload(key)`` to inspect a
       data payload's shape so you know exactly how to map it into
       block slots.
    5. ``prepare_page_script(code)`` — write Python code that builds
       block dicts from the entity store / data payloads, renders them,
       and creates the page.
    6. ``execute_page_script(language)`` — run the script, which calls
       ``render_blocks`` and ``create_page`` internally and returns the
       mutation result.

    **ExecutionContext (what your script can use):**

    - ``entities`` — ``list[dict]`` of all entities in the session
      entity store. Each dict has keys ``shared_id``, ``title``,
      ``template_name``, ``metadata`` (LLM-facing shape), ``language``,
      ``published``.
    - ``data_payload`` — ``dict`` snapshot of the session data payloads
      (a shallow copy at the time of execution; modifying it does NOT
      affect the store — use ``set_data_payload`` for that).
    - ``get_data_payload(key)`` — read a prepared payload from the session
      entity store.
    - ``set_data_payload(key, value)`` — write a prepared payload into the
      session entity store (for chaining with future script runs).
    - ``render_blocks(blocks, vibe='minimal')`` — render a list of block
      definitions (same format as the old ``create_page_from_blocks``
      ``blocks`` argument). Returns a ``RenderedPage`` object with
      ``.body`` (the clean HTML, no inline ``<style>``) and ``.css``
      (the page-scoped stylesheet). Call ``.body`` and ``.css`` when
      passing them to ``create_page`` — do NOT inline them yourself.
      Raises ``ValueError`` on validation failure; ``try/except`` and
      report the error.
    - ``create_page(title, html, language='en', css=None)`` — create a
      single page in Uwazi and return ``list[dict]`` of mutation results
      (each with ``shared_id``, ``success``, ``url``, ``error``). The
      ``html`` is the page body (typically ``render_blocks(blocks).body``)
      and the optional ``css`` is the page stylesheet (typically
      ``render_blocks(blocks).css``). Uwazi stores them separately as
      ``metadata.content`` and ``metadata.css`` — never embed ``<style>``
      tags inside ``html``; that would trigger a React 18 hydration error
      in the public page.
    - Standard libraries: ``json``, ``re``, ``collections``, ``itertools``,
      ``datetime``, ``math``.

    **Script contract:**

    - Your code must set a ``result`` variable (a ``str``) with the final
      output — typically the mutation result summary or an error message.
    - The ``result`` string is HARD-CAPPED at
      ``configuration.PYTHON_SCRIPT_OUTPUT_CHARACTERS_LIMIT`` characters.
      Be concise: report ``shared_id``, ``url``, and a short status.
    - The script runs in a temporary event loop. The helpers
      (``create_page``, ``render_blocks``) are synchronous wrappers around
      async calls — you do NOT need ``await`` or ``asyncio`` in your script.
    - Handle errors with ``try/except`` and produce a clear error message
      in ``result`` rather than letting the script crash.

    **Example script for a timeline page:**

    .. code-block:: python

        # Read prepared data
        entries = get_data_payload("timeline_entries")
        if not entries:
            result = "Error: No timeline_entries payload found."
        else:
            # Build a timeline block from the data
            blocks = [
                {
                    "type": "timeline",
                    "slots": {
                        "heading": "Books by date added",
                        "entries": entries,
                    },
                }
            ]
            try:
                rendered = render_blocks(blocks, vibe="warm")
                mutation = create_page(
                    "My Timeline",
                    rendered.body,
                    language="en",
                    css=rendered.css,
                )
                r = mutation[0]
                if r["success"]:
                    result = f"Page created: {r['shared_id']} ({r['url']})"
                else:
                    result = f"Error creating page: {r['error']}"
            except ValueError as e:
                result = f"Error rendering blocks: {e}"

    **Example script for a catalog page (card_grid from entities):**

    .. code-block:: python

        cards = [
            {
                "title": e["title"],
                "description": e["metadata"].get("description", ""),
                "image_url": e["metadata"].get("image", ""),
            }
            for e in entities[:12]
        ]
        blocks = [
            {
                "type": "hero",
                "slots": {"heading": "Our Collection", "subheading": f"{len(entities)} items"},
            },
            {
                "type": "card_grid",
                "slots": {"heading": "Browse", "cards": cards},
            },
        ]
        try:
            rendered = render_blocks(blocks)
            mutation = create_page("Catalog", rendered.body, css=rendered.css)
            result = f"Catalog page created: {mutation[0]['shared_id']}"
        except ValueError as e:
            result = f"Rendering error: {e}"

    Args:
        code: Python source code conforming to the script contract
            above. Must set a ``result`` string variable.

    Returns:
        Confirmation message, or ``Error:`` message if the code has
        a syntax error.
    """
    try:
        compile(code, "<page_script>", "exec")
    except SyntaxError as exc:
        return f"Error: Syntax error in page script:\n{exc}"

    ctx.deps.entity_store.set_data_payload(_PENDING_SCRIPT_KEY, code)
    return "Page script stored. Call ``execute_page_script(language=...)`` to run it and create the page in Uwazi."


async def execute_page_script(
    ctx: RunContext[UwaziAgentToolsDependencies],
    language: str = "en",
) -> str:
    """Execute the pending page-building script and create the page in Uwazi.

    Call this AFTER ``prepare_page_script`` to run the stored script.
    The script reads session data, builds block definitions, renders HTML
    via the page-builder engine, and creates a page in Uwazi.

    **Typical workflow:**

    ``prepare_page_script`` → inspect / iterate → ``execute_page_script``

    The script's ``result`` variable is returned as the tool output
    (subject to the same hard character cap as ``run_python_code``).

    If no script has been stored (``prepare_page_script`` was not called),
    returns an error message.

    Args:
        language: ISO 639-1 language code for the new page.
            Defaults to ``en``.

    Returns:
        The value of the script's ``result`` variable, or an error
        message if the script had not been stored or if execution
        failed.
    """
    if ctx.deps.page_api is None:
        return "Error: Page tools are not configured: `page_api` is missing on dependencies."
    if ctx.deps.page_builder_dir is None:
        return "Error: Page builder is not configured: `page_builder_dir` is missing on dependencies."

    code: str | None = ctx.deps.entity_store.get_data_payload(_PENDING_SCRIPT_KEY)  # type: ignore[assignment]
    if code is None:
        return (
            "Error: No pending page script. Call ``prepare_page_script(code)`` "
            "first to write the script, then call this tool to run it."
        )

    ctx.deps.entity_store.set_data_payload(_PENDING_SCRIPT_KEY, None)

    entities = ctx.deps.entity_store.entities
    page_api: PageApiPort = ctx.deps.page_api
    page_builder_dir: Path = ctx.deps.page_builder_dir
    entity_store: EntityStore = ctx.deps.entity_store

    output = await asyncio.to_thread(
        _execute_script_in_thread,
        code,
        entities,
        page_api,
        language,
        entity_store,
        page_builder_dir,
    )
    limit = configuration.PYTHON_SCRIPT_OUTPUT_CHARACTERS_LIMIT
    if len(output) > limit:
        logger.warning(
            "Page script output truncated from {} to {} characters",
            len(output),
            limit,
        )
        output = output[:limit] + "\n... [output truncated]"
    return output


def _execute_script_in_thread(
    code: str,
    entities: list,
    page_api: PageApiPort,
    language: str,
    entity_store: EntityStore,
    page_builder_dir: Path,
) -> str:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        renderer = PageRenderer(page_builder_dir)

        def render_blocks(blocks: list[dict], vibe: str = "minimal") -> "RenderedPage":
            resolved = _resolve_vibe(vibe)
            return RenderedPage(
                body=renderer.render_body(vibe=resolved, blocks=blocks),
                css=renderer.render_css(vibe=resolved, blocks=blocks),
            )

        def create_page(
            title: str,
            html: str,
            language: str | None = None,
            css: str | None = None,
        ) -> list[dict]:
            lang = language or _default_language
            page = AgentPageCreate(
                title=title,
                content=html,
                css=css,
                language=lang,
            )
            results = loop.run_until_complete(page_api.create_pages([page], lang))
            return [r.model_dump() for r in results]

        _default_language = language

        data_payload = dict(entity_store.data_payload)
        data_payload.pop(_PENDING_SCRIPT_KEY, None)

        namespace: dict[str, Any] = {
            "entities": [e.model_dump() for e in entities],
            "data_payload": data_payload,
            "get_data_payload": entity_store.get_data_payload,
            "set_data_payload": entity_store.set_data_payload,
            "RenderedPage": RenderedPage,
            "render_blocks": render_blocks,
            "create_page": create_page,
            "json": json,
            "re": re,
            "collections": collections,
            "itertools": itertools,
            "datetime": datetime,
            "math": math,
        }

        try:
            exec(code, namespace)
        except Exception as exc:
            tb = traceback.format_exc()
            return f"Error executing page script: {type(exc).__name__}: {exc}\n\nTraceback:\n{tb}"

        result = namespace.get("result")
        if result is None:
            return (
                "Page script executed successfully but no ``result`` variable "
                "was set. Set ``result = 'your output string'``."
            )
        return str(result)
    finally:
        loop.close()
