import asyncio
import collections
import contextlib
import io
import itertools
import json
import math
import re
import traceback
from datetime import datetime
from typing import Any

from loguru import logger
from pydantic_ai import RunContext

from uwazi_agent import configuration
from uwazi_agent.domain.agent_entity import AgentEntity
from uwazi_agent.domain.agent_entity_create import AgentEntityCreate
from uwazi_agent.domain.agent_relationship_create import AgentRelationshipCreate
from uwazi_agent.ports.entity_api_port import EntityApiPort
from uwazi_agent.ports.relationship_api_port import RelationshipApiPort
from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies
from uwazi_agent.use_cases.tools.tool_call_cache import ToolCallCache

_ENTITY_READ_TOOLS = {
    "query_entities",
    # Kept for back-compat with previously-saved tool cache entries
    # written before the entity read tools were merged. Safe to drop on
    # the next major version bump.
    "search_entities_by_text",
    "search_entities_by_filter",
    "get_entities_by_shared_ids",
    "get_entities_by_template",
}


def _build_sync_crud_functions(
    entity_api: EntityApiPort,
    relationship_api: RelationshipApiPort | None,
    default_language: str,
    loop: asyncio.AbstractEventLoop,
    tool_cache: ToolCallCache,
) -> tuple:
    def create_entities(entities_dicts: list[dict], language: str | None = None) -> list[dict]:
        lang = language or default_language
        entities = [AgentEntityCreate(**e) for e in entities_dicts]
        results = loop.run_until_complete(entity_api.create_entities(entities, lang))
        tool_cache.invalidate_tools(_ENTITY_READ_TOOLS)
        return [r.model_dump() for r in results]

    def update_entities(entities_dicts: list[dict], language: str | None = None) -> list[dict]:
        lang = language or default_language
        entities = [AgentEntity(**e) for e in entities_dicts]
        results = loop.run_until_complete(entity_api.update_entities(entities, lang))
        tool_cache.invalidate_tools(_ENTITY_READ_TOOLS)
        return [r.model_dump() for r in results]

    def delete_entities(shared_ids: list[str]) -> list[dict]:
        results = loop.run_until_complete(entity_api.delete_entities_by_shared_ids(shared_ids))
        tool_cache.invalidate_tools(_ENTITY_READ_TOOLS)
        return [r.model_dump() for r in results]

    def set_publish_status(shared_ids: list[str], published: bool) -> list[dict]:
        results = loop.run_until_complete(entity_api.set_entities_publish_status(shared_ids=shared_ids, published=published))
        tool_cache.invalidate_tools(_ENTITY_READ_TOOLS)
        return [r.model_dump() for r in results]

    def publish_entities(shared_ids: list[str]) -> dict:
        results = set_publish_status(shared_ids, True)
        return {
            "success_count": sum(1 for r in results if r["success"]),
            "failure_count": sum(1 for r in results if not r["success"]),
            "rate_limited": [r["shared_id"] for r in results if r.get("error_code") == "RATE_LIMITED"],
            "permission_denied": [r["shared_id"] for r in results if r.get("error_code") == "PERMISSION_DENIED"],
            "not_found": [r["shared_id"] for r in results if r.get("error_code") == "NOT_FOUND"],
            "errors": [r for r in results if not r["success"]],
        }

    def unpublish_entities(shared_ids: list[str]) -> dict:
        results = set_publish_status(shared_ids, False)
        return {
            "success_count": sum(1 for r in results if r["success"]),
            "failure_count": sum(1 for r in results if not r["success"]),
            "rate_limited": [r["shared_id"] for r in results if r.get("error_code") == "RATE_LIMITED"],
            "permission_denied": [r["shared_id"] for r in results if r.get("error_code") == "PERMISSION_DENIED"],
            "not_found": [r["shared_id"] for r in results if r.get("error_code") == "NOT_FOUND"],
            "errors": [r for r in results if not r["success"]],
        }

    def create_relationships(relationships_dicts: list[dict], language: str | None = None) -> list[dict]:
        lang = language or default_language
        if relationship_api is None:
            return [{"success": False, "error": "Relationship API not configured"}]
        rels = [AgentRelationshipCreate(**r) for r in relationships_dicts]
        results = loop.run_until_complete(relationship_api.create_relationships(rels, lang))
        return [r.model_dump() for r in results]

    return (
        create_entities,
        update_entities,
        delete_entities,
        publish_entities,
        unpublish_entities,
        set_publish_status,
        create_relationships,
    )


def _execute_python_code(
    code: str,
    entities: list[AgentEntity],
    entity_api: EntityApiPort,
    relationship_api: RelationshipApiPort | None,
    language: str,
    tool_cache: ToolCallCache,
) -> str:
    # Create a dedicated event loop for this thread so CRUD helpers can run
    # async port methods without calling asyncio.run() inside a running loop.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        create_fn, update_fn, delete_fn, publish_fn, unpublish_fn, set_publish_fn, create_rel_fn = (
            _build_sync_crud_functions(entity_api, relationship_api, language, loop, tool_cache)
        )

        namespace: dict[str, Any] = {
            "entities": [e.model_dump() for e in entities],
            "create_entities": create_fn,
            "update_entities": update_fn,
            "delete_entities": delete_fn,
            "publish_entities": publish_fn,
            "unpublish_entities": unpublish_fn,
            "set_publish_status": set_publish_fn,
            "create_relationships": create_rel_fn,
            "json": json,
            "re": re,
            "collections": collections,
            "itertools": itertools,
            "datetime": datetime,
            "math": math,
        }

        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                exec(code, namespace)
        except Exception as e:
            tb = traceback.format_exc()
            return f"Error executing code: {type(e).__name__}: {e}\n\nTraceback:\n{tb}"

        result = namespace.get("result")
        if result is None:
            return "Code executed successfully but no 'result' variable was set. Set result = 'your output string'."
        return str(result)
    finally:
        loop.close()


async def run_python_code(
    ctx: RunContext[UwaziAgentToolsDependencies],
    code: str,
    language: str = "en",
) -> str:
    """Execute Python code that processes entities stored in the session entity store.

    The execution environment provides:
    - ``entities``: list of dicts, one per entity in the session store. Every entity
      has these top-level keys (some values may be ``None`` or empty):
        * ``shared_id`` (str) — stable entity id, ALWAYS set.
        * ``title`` (str) — entity title, ALWAYS set (may be empty string).
        * ``template_name`` (str) — entity's template name, ALWAYS set.
        * ``metadata`` (dict) — the template's properties; see 'Metadata value
          shapes' below for the per-type value contract.
        * ``language`` (str) — ISO 639-1 (e.g. ``"en"``), ALWAYS set.
        * ``published`` (bool | None) — READ-ONLY mirror of Uwazi's stored flag;
          setting it on an entity dict has NO side effect on the server. To
          publish or unpublish, use the helpers below.
        * ``creation_date`` (str | None) — ISO 8601 UTC timestamp (e.g.
          ``'2024-03-15T10:22:33Z'``) or ``None`` if Uwazi did not record one.
          Use this for 'first/last added', 'oldest', 'most recent' questions.
        * ``edit_date`` (str | None) — same shape as ``creation_date``.

      ``metadata`` is a dict whose keys match the entity's TEMPLATE properties.
      Three rules you MUST follow when reading it:
        1. A metadata key is PRESENT only if the template defines that property
           AND the entity actually carries a value. The mapper drops properties
           that are not in the template and never inserts a placeholder for
           missing values. A 'required' template property that an entity never
           set is ABSENT from the dict — guard every read with
           ``e['metadata'].get('key')``, never ``e['metadata']['key']``.
        2. 'Missing' has two flavours the agent must treat the same: the key is
           NOT in the dict (template property was never set), or the key IS in
           the dict with value ``None`` (entity was saved with no value).
           Use ``e['metadata'].get('key') is None`` to test, or the
           ``has_value(e, 'key')`` idiom further down.
        3. The VALUE shape depends on the template property's type. The full
           per-type table is in 'Metadata value shapes' below; a worked example
           for the current store is in the pre-computed 'Entity store shape'
           block the orchestrator injects at delegation time. Trust the runtime
           value, not the schema, when they disagree (legacy entities can carry
           keys the current template does not define).
    - ``create_entities(entities_dicts, language='en')``: Create new entities. Each dict
      must have 'title' and 'template_name'. Returns list of mutation result dicts.
    - ``update_entities(entities_dicts, language='en')``: Update existing entities. Each
      dict must have 'shared_id' and 'template_name'. Returns list of mutation result dicts.
    - ``delete_entities(shared_ids)``: Delete entities by shared_id list. Returns list of
      mutation result dicts.
    - ``publish_entities(shared_ids)``: Make entities public (visible to anonymous
      users). Returns list of mutation result dicts.
    - ``unpublish_entities(shared_ids)``: Make entities private again (visible only
      to logged-in users with permission). Returns list of mutation result dicts.
    - ``set_publish_status(shared_ids, published)``: General form of the two above;
      pass ``published=True`` to publish, ``False`` to unpublish. Returns list of
      mutation result dicts.
    - ``create_relationships(relationships_dicts, language='en')``: Create entity-to-entity
      relationships. Each dict must have ``from_entity_shared_id``,
      ``to_entity_shared_id``, and ``relationship_type_name``. Optionally provide
      ``file_id`` and ``reference_text`` for document-anchored relationships.
      Returns list of mutation result dicts.
    - Standard libraries: json, re, collections, itertools, datetime, math.

    The code must set a ``result`` variable with the output string.

    IMPORTANT — Read the entity-store shape block before introspecting entities:
    The orchestrator injects an 'Entity store shape' block at the top of your
    delegation. It already lists the top-level keys, the per-template metadata
    schema (with types, sample values, and nullability), and the earliest/latest
    entity by ``creation_date``. Read that block FIRST; do NOT spend your first
    ``run_python_code`` call printing ``list(e.keys())`` and
    ``e['metadata'].keys()`` to discover what is in the store. A discovery call
    that only inspects keys is a wasted iteration — the shape block already
    has that information. The only case where ``entities[0]['metadata']``
    inspection is justified is a property the shape block does not list
    (template drift, legacy keys).

    Idioms:
        def has_value(e, key):
            md = e.get('metadata') or {}
            v = md.get(key)
            if v is None: return False
            if isinstance(v, str) and v == '': return False
            if isinstance(v, (list, dict)) and len(v) == 0: return False
            return True

        # First entity by creation_date (entities that lack one are skipped):
        dated = [e for e in entities if e.get('creation_date')]
        if dated:
            first = min(dated, key=lambda e: e['creation_date'])

    IMPORTANT — Output must be minimal:
    The ``result`` string MUST be as concise as possible while still being understandable.
    Return ONLY the data the user asked for, formatted compactly (e.g. comma-separated
    titles, bullet lists, or short summaries). Never dump full entity payloads, metadata,
    or debugging info. Example: for "give me all titles", set
    ``result = '\n'.join(e['title'] for e in entities)``.

    IMPORTANT — Hard output cap:
    The returned string is HARD-CAPPED at
    ``configuration.PYTHON_SCRIPT_OUTPUT_CHARACTERS_LIMIT`` characters (the
    current value lives in ``uwazi_agent.configuration``). Any output past
    that limit is silently truncated and replaced with
    ``"\n... [output truncated]"``. The full output is NOT available later —
    the Python agent has no way to fetch the truncated tail. Therefore the
    ``result`` string MUST be designed to fit within this cap. If the natural
    answer would exceed it, return a summary (count, first N items, aggregated
    stats) instead of the raw data. The orchestrator that reads this tool's
    output will also assume the answer is complete, so anything lost to
    truncation is lost to the whole conversation.

    Args:
        code: Python code to execute. Must set ``result`` to a string.
        language: ISO 639-1 language code for CRUD operations. Defaults to "en".

    Returns:
        The value of the ``result`` variable, or an error message.
    """
    if ctx.deps.entity_api is None:
        return "Error: Entity tools are not configured: `entity_api` is missing on dependencies."

    entities = ctx.deps.entity_store.entities

    output = await asyncio.to_thread(
        _execute_python_code, code, entities, ctx.deps.entity_api, ctx.deps.relationship_api, language, ctx.deps.tool_cache
    )
    limit = configuration.PYTHON_SCRIPT_OUTPUT_CHARACTERS_LIMIT
    if len(output) > limit:
        logger.warning("Output truncated from {} to {} characters", len(output), limit)
        output = output[:limit] + "\n... [output truncated]"
    return output
