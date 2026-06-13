import functools
from typing import Any, Callable

from loguru import logger
from pydantic_ai import Agent, RunContext
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.models import Model
from pydantic_ai.tools import Tool

from .instructions import (
    ENTITY_INSTRUCTIONS,
    ORCHESTRATOR_INSTRUCTIONS,
    PAGE_INSTRUCTIONS,
    PYTHON_INSTRUCTIONS,
    TEMPLATES_INSTRUCTIONS,
)
from .tools.agent_context import get_current_agent, set_current_agent
from .tools.create_entities import create_entities
from .tools.create_relationship_type import create_relationship_type
from .tools.delete_page_menu_links import delete_page_menu_links
from .tools.create_template import create_template
from .tools.create_thesauri import create_thesauri
from ..logging_config import truncate_log_message
from .tools.delete_entities_by_shared_ids import delete_entities_by_shared_ids
from .tools.delete_pages_by_shared_ids import delete_pages_by_shared_ids
from .tools.delete_relationship_type import delete_relationship_type
from .tools.delete_template import delete_template
from .tools.delete_thesauri import delete_thesauri
from .tools.dependencies import UwaziAgentToolsDependencies
from .tools.entity_store_shape import build_entity_store_shape_block
from .tools.get_entities_by_shared_ids import get_entities_by_shared_ids
from .tools.get_entities_by_template import get_entities_by_template
from .tools.get_entity_store_status import get_entity_store_status
from .tools.get_pages_by_shared_ids import get_pages_by_shared_ids
from .tools.search_entities_by_filter import search_entities_by_filter
from .tools.search_entities_by_text import search_entities_by_text
from .tools.get_publish_status import get_publish_status
from .tools.get_relationship_type_names import get_relationship_type_names
from .tools.get_template_names import list_templates
from .tools.get_templates_by_names import get_templates_by_names
from .tools.get_thesauris_by_names import get_thesauris_by_names
from .tools.get_thesauris_names import list_thesauri
from .tools.get_languages import get_languages
from .tools.list_pages import list_pages
from .tools.page_script_executor import execute_page_script, prepare_page_script
from .tools.python_code_executor import run_python_code
from .tools.query_entities import query_entities
from .tools.set_entities_publish_status import set_entities_publish_status
from .tools.update_entities import update_entities
from .tools.update_pages import update_pages
from .tools.update_relationship_type import update_relationship_type
from .tools.update_template import update_template
from .tools.update_thesauri import update_thesauri

_TEMPLATE_READ_TOOLS = {"list_templates", "get_templates_by_names"}
_THESAURI_READ_TOOLS = {"list_thesauri", "get_thesauris_by_names"}
_RELATIONSHIP_READ_TOOLS = {"get_relationship_type_names"}
_ENTITY_READ_TOOLS = {"query_entities"}
_PAGE_READ_TOOLS = {
    "list_pages",
    "get_pages_by_shared_ids",
}
_LANGUAGE_READ_TOOLS = {"get_languages"}
_STATS_READ_TOOLS = {"list_templates", "list_thesauri", "get_thesauris_by_names"}

_WRITE_INVALIDATION_MAP: dict[str, tuple[set[str], Callable | None]] = {
    "create_template": (
        _TEMPLATE_READ_TOOLS | _STATS_READ_TOOLS,
        None,
    ),
    "update_template": (
        _TEMPLATE_READ_TOOLS | _STATS_READ_TOOLS,
        lambda deps: deps.schema_store.clear_templates(),
    ),
    "delete_template": (
        _TEMPLATE_READ_TOOLS | _STATS_READ_TOOLS,
        lambda deps: deps.schema_store.clear_templates(),
    ),
    "create_thesauri": (
        _THESAURI_READ_TOOLS | _STATS_READ_TOOLS,
        lambda deps: deps.schema_store.clear_thesauri(),
    ),
    "update_thesauri": (
        _THESAURI_READ_TOOLS | _STATS_READ_TOOLS,
        lambda deps: deps.schema_store.clear_thesauri(),
    ),
    "delete_thesauri": (
        _THESAURI_READ_TOOLS | _STATS_READ_TOOLS,
        lambda deps: deps.schema_store.clear_thesauri(),
    ),
    "create_relationship_type": (_RELATIONSHIP_READ_TOOLS, None),
    "update_relationship_type": (_RELATIONSHIP_READ_TOOLS, None),
    "delete_relationship_type": (_RELATIONSHIP_READ_TOOLS, None),
    "create_entities": (_ENTITY_READ_TOOLS, None),
    "update_entities": (_ENTITY_READ_TOOLS, None),
    "delete_entities_by_shared_ids": (_ENTITY_READ_TOOLS, None),
    "set_entities_publish_status": (_ENTITY_READ_TOOLS, None),
    "update_pages": (_PAGE_READ_TOOLS, None),
    "delete_pages_by_shared_ids": (_PAGE_READ_TOOLS, None),
    "delete_page_menu_links": (_PAGE_READ_TOOLS, None),
}


def _format_progress_msg(agent_name: str, tool_name: str, params: dict[str, Any]) -> str:
    name = params.get("name") or ""
    template_name = params.get("template_name") or ""
    search_term = params.get("search_term") or ""
    label = name or template_name
    quoted = f" '{label}'" if label else ""
    search = f" for '{search_term}'" if search_term else ""

    tool_label = tool_name.replace("_", " ")
    agent_label = agent_name.replace("_", " ").title() if agent_name != "orchestrator" else ""
    prefix = f"{agent_label}: " if agent_label else ""

    return f"{prefix}{tool_label}{quoted}{search}..."


class _ToolParamsFormatter:
    """Format tool-call parameters for logs without flooding the console.

    Large in-line values (multi-line Python scripts, HTML, CSS, long entity
    lists) can produce dozens or hundreds of log lines per call. This formatter
    keeps every logged parameter value to a single line and truncates very
    long strings so the whole tool-call log stays scannable.
    """

    _MAX_LEN = 120

    @classmethod
    def format(cls, params: dict[str, Any]) -> str:
        if not params:
            return ""
        items = []
        for key, value in params.items():
            formatted = cls._format_value(value)
            items.append(f"{key}={formatted}")
        return ", ".join(items)

    @classmethod
    def _format_value(cls, value: Any) -> str:
        if isinstance(value, str):
            return cls._format_str(value)
        if isinstance(value, (list, tuple)):
            return cls._format_list(value)
        if isinstance(value, dict):
            return cls._format_dict(value)
        text = str(value)
        return cls._maybe_truncate(text)

    @classmethod
    def _format_str(cls, text: str) -> str:
        single_line = " ".join(text.splitlines())
        if not single_line:
            return "''"
        return cls._maybe_truncate(single_line)

    @classmethod
    def _format_list(cls, value: list | tuple) -> str:
        if not value:
            return "[]"
        parts = [cls._format_value(item) for item in value]
        joined = ", ".join(parts)
        inner = cls._maybe_truncate(joined)
        return f"[{inner}]"

    @classmethod
    def _format_dict(cls, value: dict) -> str:
        if not value:
            return "{}"
        items = [f"{k}={cls._format_value(v)}" for k, v in value.items()]
        joined = ", ".join(items)
        inner = cls._maybe_truncate(joined)
        return f"{{{inner}}}"

    @classmethod
    def _maybe_truncate(cls, text: str) -> str:
        if len(text) > cls._MAX_LEN:
            return text[: cls._MAX_LEN - 1] + "…"
        return text


def _extract_params(args: tuple, kwargs: dict, func: Callable) -> dict[str, Any]:
    import inspect

    sig = inspect.signature(func)
    params = list(sig.parameters.keys())
    result: dict[str, Any] = {}
    for i, arg in enumerate(args):
        if i < len(params):
            result[params[i]] = arg
    result.update(kwargs)
    return result


def _normalize_cache_params(tool_name: str, params: dict[str, Any]) -> dict[str, Any]:
    """Normalize params for cache-key purposes so that semantically-equivalent
    calls share the same cache entry."""
    if tool_name == "query_entities" and params.get("mode") == "by_template" and "limit" in params:
        params = {**params, "limit": 10000}
    return params


def _wrap_read_tool(func: Callable) -> Callable:
    @functools.wraps(func)
    async def wrapper(ctx: RunContext[UwaziAgentToolsDependencies], *args: Any, **kwargs: Any) -> Any:
        agent_name = get_current_agent()
        params = _extract_params(args, kwargs, func)
        cache_params = _normalize_cache_params(func.__name__, params)
        cached = ctx.deps.tool_cache.get(func.__name__, cache_params)
        params_str = _ToolParamsFormatter.format(params)
        if cached is not None:
            logger.info("[{}] CACHE HIT: {}({})", agent_name, func.__name__, params_str)
            return cached
        logger.info("[{}] CALLING: {}({})", agent_name, func.__name__, params_str)
        ctx.deps.tool_progress.append(_format_progress_msg(agent_name, func.__name__, params))
        result = await func(ctx, *args, **kwargs)
        if not isinstance(result, str) or not result.startswith("Error"):
            ctx.deps.tool_cache.set(func.__name__, cache_params, result)
        return result

    return wrapper


def _wrap_write_tool(func: Callable) -> Callable:
    tool_name = func.__name__
    invalidated_tools, schema_invalidator = _WRITE_INVALIDATION_MAP.get(tool_name, (set(), None))

    @functools.wraps(func)
    async def wrapper(ctx: RunContext[UwaziAgentToolsDependencies], *args: Any, **kwargs: Any) -> Any:
        agent_name = get_current_agent()
        params = _extract_params(args, kwargs, func)
        params_str = _ToolParamsFormatter.format(params)
        logger.info("[{}] CALLING: {}({})", agent_name, tool_name, params_str)
        ctx.deps.tool_progress.append(_format_progress_msg(agent_name, tool_name, params))
        result = await func(ctx, *args, **kwargs)
        is_error = isinstance(result, str) and result.startswith("Error")
        if not is_error:
            if invalidated_tools:
                ctx.deps.tool_cache.invalidate_tools(invalidated_tools)
                logger.info(
                    "[{}] CACHE INVALIDATED: {} -> {}",
                    agent_name,
                    tool_name,
                    invalidated_tools,
                )
            if schema_invalidator is not None:
                schema_invalidator(ctx.deps)
            # Evict any entities touched by this mutation from the session
            # entity store's trim cache, so the next ``query_entities(by_ids)``
            # call re-fetches them from Uwazi instead of serving stale data.
            if tool_name in {"update_entities", "delete_entities_by_shared_ids", "set_entities_publish_status"}:
                _evict_entity_store_ids(ctx.deps, params, result)
            elif tool_name == "create_entities":
                _evict_entity_store_ids(ctx.deps, params, result)
        return result

    return wrapper


def _evict_entity_store_ids(deps, params: dict[str, Any], result: Any) -> None:
    """Drop trim-cache and entity-store entries for ids touched by a mutation.

    Looks for ``shared_ids`` (publish, delete) and ``updates`` (update) in
    the call args, and for ``shared_id`` fields in the result list
    (covers both successful and failed create results).
    """
    ids: set[str] = set()
    shared_ids = params.get("shared_ids") or []
    ids.update(sid for sid in shared_ids if isinstance(sid, str))
    updates = params.get("updates") or []
    for upd in updates:
        sid = getattr(upd, "shared_id", None)
        if isinstance(sid, str) and sid:
            ids.add(sid)
    if isinstance(result, list):
        for item in result:
            sid = getattr(item, "shared_id", None)
            if isinstance(sid, str) and sid:
                ids.add(sid)
    if ids:
        deps.entity_store.invalidate_ids(sorted(ids))


def _read_tool(func: Callable) -> Tool:
    return Tool(_wrap_read_tool(func), takes_ctx=True)


def _write_tool(func: Callable) -> Tool:
    return Tool(_wrap_write_tool(func), takes_ctx=True)


def build_templates_tools() -> list[Tool]:
    return [
        _read_tool(get_languages),
        _read_tool(get_thesauris_by_names),
        _read_tool(list_thesauri),
        _write_tool(create_thesauri),
        _write_tool(update_thesauri),
        _write_tool(delete_thesauri),
        _read_tool(get_relationship_type_names),
        _write_tool(create_relationship_type),
        _write_tool(update_relationship_type),
        _write_tool(delete_relationship_type),
        _read_tool(get_templates_by_names),
        _read_tool(list_templates),
        _write_tool(create_template),
        _write_tool(update_template),
        _write_tool(delete_template),
    ]


def build_entity_tools() -> list[Tool]:
    return [
        _read_tool(get_languages),
        _read_tool(query_entities),
        _read_tool(get_publish_status),
        _write_tool(create_entities),
        _write_tool(update_entities),
        _write_tool(set_entities_publish_status),
        _write_tool(delete_entities_by_shared_ids),
    ]


def build_page_tools() -> list[Tool]:
    """Tools for the page sub-agent.

    The page agent is responsible for creating, updating and deleting pages.
    It does NOT fetch or mutate entities; entity data is supplied by the
    orchestrator through the shared session entity store (``entities`` and
    ``data_payload``), which page scripts can read via
    ``prepare_page_script`` / ``execute_page_script``.

    Pages are always built with the block-template system inside a Python
    script. The only page-creation path is ``prepare_page_script`` followed
    by ``execute_page_script``. No custom-HTML/JS or direct block tools are
    exposed to the page agent.
    """
    return [
        _read_tool(list_pages),
        _read_tool(get_pages_by_shared_ids),
        _write_tool(prepare_page_script),
        _write_tool(execute_page_script),
        _write_tool(update_pages),
        _write_tool(delete_pages_by_shared_ids),
        _write_tool(delete_page_menu_links),
    ]


def build_python_tools() -> list[Tool]:
    return [
        _write_tool(run_python_code),
        _write_tool(set_entities_publish_status),
        _read_tool(search_entities_by_text),
        _read_tool(search_entities_by_filter),
        _read_tool(get_entities_by_template),
        _read_tool(get_entities_by_shared_ids),
        _read_tool(get_publish_status),
        Tool(get_entity_store_status, takes_ctx=True),
    ]


def build_templates_agent(model: Model) -> Agent[UwaziAgentToolsDependencies, str]:
    return Agent(
        model,
        deps_type=UwaziAgentToolsDependencies,
        instructions=TEMPLATES_INSTRUCTIONS,
        tools=build_templates_tools(),
    )


def build_entity_agent(model: Model) -> Agent[UwaziAgentToolsDependencies, str]:
    return Agent(
        model,
        deps_type=UwaziAgentToolsDependencies,
        instructions=ENTITY_INSTRUCTIONS,
        tools=build_entity_tools(),
    )


def build_page_agent(model: Model) -> Agent[UwaziAgentToolsDependencies, str]:
    return Agent(
        model,
        deps_type=UwaziAgentToolsDependencies,
        instructions=PAGE_INSTRUCTIONS,
        tools=build_page_tools(),
    )


def build_python_agent(model: Model) -> Agent[UwaziAgentToolsDependencies, str]:
    return Agent(
        model,
        deps_type=UwaziAgentToolsDependencies,
        instructions=PYTHON_INSTRUCTIONS(),
        tools=build_python_tools(),
    )


def _make_delegation_tool(
    sub_agent: Agent[UwaziAgentToolsDependencies, str],
    name: str,
    description: str,
    *,
    use_page_prompt_context: bool = False,
    inject_entity_store_shape: bool = False,
) -> Tool:
    """Build a delegate-to-X tool.

    When ``use_page_prompt_context`` is True, the delegation injects the
    page-builder section of ``SchemaStore`` into the sub-agent's prompt
    (via :meth:`SchemaStore.to_page_prompt_context`). Use this for the
    page sub-agent only — the entity / schema / python agents never
    see the page-block library.

    When ``inject_entity_store_shape`` is True, the delegation injects a
    pre-computed **entity-store shape block** (top-level keys, per-
    template metadata schema, nullability per key, earliest/latest by
    ``creation_date``) into the sub-agent's prompt via
    :func:`build_entity_store_shape_block`. Use this for the Python
    sub-agent only — it is the only sub-agent that iterates
    ``entities`` directly, and the block lets it answer questions
    about the store without a wasteful introspective
    ``run_python_code`` call.
    """
    agent_label = name.replace("delegate_to_", "")

    async def delegate(ctx: RunContext[UwaziAgentToolsDependencies], task: str) -> str:
        parent_agent = get_current_agent()
        logger.info(
            "[{}] DELEGATING to {} (task: {}...)",
            parent_agent,
            agent_label,
            truncate_log_message(task[:100]),
        )
        ctx.deps.tool_progress.append(f"Delegating to {agent_label} agent...")
        set_current_agent(agent_label)
        try:
            # Inject the schema + entity context the sub-agent needs. The
            # schema store also carries the pre-loaded "Available context"
            # snapshot (languages, template names+counts, thesaurus names,
            # relationship type names) which is appended below.
            available_context = ctx.deps.schema_store.to_available_context()
            if use_page_prompt_context:
                schema_context = ctx.deps.schema_store.to_page_prompt_context()
            else:
                schema_context = ctx.deps.schema_store.to_prompt_context()
            entity_context = ctx.deps.entity_store.to_context_summary()
            context_parts = [p for p in [available_context, schema_context, entity_context] if p]
            # Pre-computed shape block for the entity store: lets the
            # Python sub-agent answer shape-aware questions
            # ("first/last Book", "what does the metadata look like?")
            # in a single run_python_code call. Only emitted when
            # ``inject_entity_store_shape`` is True (i.e. for the
            # python_agent). The block is omitted when the store is
            # empty — the agent has nothing to process.
            if inject_entity_store_shape:
                shape_block = build_entity_store_shape_block(
                    entity_store=ctx.deps.entity_store,
                    schema_templates=ctx.deps.schema_store.templates,
                )
                if shape_block:
                    context_parts.append(shape_block)
            context_str = "\n\n".join(context_parts)
            enriched_task = f"{context_str}\n\n{task}" if context_parts else task
            result = await sub_agent.run(enriched_task, deps=ctx.deps, usage=ctx.usage)
            logger.info("[{}] DELEGATION COMPLETE", agent_label)
            return result.output
        except UsageLimitExceeded as exc:
            logger.error(
                "[{}] DELEGATION BUDGET EXHAUSTED: {}",
                agent_label,
                truncate_log_message(str(exc)),
            )
            return (
                f"Sub-agent budget exhausted ({exc}). "
                f"The task was too complex or the agent entered an error loop. "
                f"Try breaking it into smaller steps and retrying."
            )
        except Exception as exc:
            logger.error(
                "[{}] DELEGATION FAILED: {}",
                agent_label,
                truncate_log_message(str(exc)),
            )
            return f"Sub-agent error ({name}): {exc}. Please rephrase the task or break it into smaller steps and retry."
        finally:
            set_current_agent(parent_agent)

    delegate.__name__ = name
    delegate.__qualname__ = name
    delegate.__doc__ = description
    return Tool(delegate, takes_ctx=True, name=name, description=description)


def build_orchestrator(
    model: Model,
    schema_agent: Agent[UwaziAgentToolsDependencies, str],
    entity_agent: Agent[UwaziAgentToolsDependencies, str],
    page_agent: Agent[UwaziAgentToolsDependencies, str],
    python_agent: Agent[UwaziAgentToolsDependencies, str],
) -> Agent[UwaziAgentToolsDependencies, str]:
    delegation_tools = [
        _make_delegation_tool(
            schema_agent,
            "delegate_to_schema_agent",
            "Delegate schema mutation tasks (create, update, delete thesauri, templates, "
            "relationship types) to the schema sub-agent. Do NOT use this for reading schema "
            "data — use the read tools directly instead.",
        ),
        _make_delegation_tool(
            entity_agent,
            "delegate_to_entity_agent",
            "Delegate entity mutation tasks (create, update, delete, publish/unpublish "
            "entities) to the entity sub-agent. Do NOT use this for reading entities — "
            "use the read tools directly instead.",
        ),
        _make_delegation_tool(
            page_agent,
            "delegate_to_page_agent",
            "Delegate page mutation tasks (create, update, delete pages) to "
            "the page sub-agent. New pages are automatically registered in "
            "Settings → Links so they are reachable from the public navigation. "
            "Do NOT use this for reading pages — use the read tools directly instead.",
            use_page_prompt_context=True,
        ),
        _make_delegation_tool(
            python_agent,
            "delegate_to_python_agent",
            "Delegate batch processing of large entity sets to the Python sub-agent. "
            "Use this when the entity agent reports that entities exceed the LLM limit of 5. "
            "The Python agent is the ONLY agent allowed to process more than 5 entities. "
            "It generates and executes Python code with CRUD capabilities "
            "on entities stored in the session entity store.",
            inject_entity_store_shape=True,
        ),
    ]

    read_tools = [
        _read_tool(list_templates),
        _read_tool(get_templates_by_names),
        _read_tool(list_thesauri),
        _read_tool(get_thesauris_by_names),
        _read_tool(get_relationship_type_names),
        _read_tool(get_languages),
        _read_tool(query_entities),
        _read_tool(get_publish_status),
        _read_tool(list_pages),
        _read_tool(get_pages_by_shared_ids),
        Tool(get_entity_store_status, takes_ctx=True),
    ]

    all_tools = delegation_tools + read_tools

    return Agent(
        model,
        deps_type=UwaziAgentToolsDependencies,
        instructions=ORCHESTRATOR_INSTRUCTIONS(),
        tools=all_tools,
    )


def build_uwazi_agents(
    model: Model,
) -> Agent[UwaziAgentToolsDependencies, str]:
    return build_orchestrator(
        model=model,
        schema_agent=build_templates_agent(model),
        entity_agent=build_entity_agent(model),
        page_agent=build_page_agent(model),
        python_agent=build_python_agent(model),
    )
