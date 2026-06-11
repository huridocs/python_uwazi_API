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
from .tools.create_pages import create_pages
from .tools.create_relationship_type import create_relationship_type
from .tools.create_template import create_template
from .tools.create_thesauri import create_thesauri
from .tools.delete_entities_by_shared_ids import delete_entities_by_shared_ids
from .tools.delete_pages_by_shared_ids import delete_pages_by_shared_ids
from .tools.delete_relationship_type import delete_relationship_type
from .tools.delete_template import delete_template
from .tools.delete_thesauri import delete_thesauri
from .tools.dependencies import UwaziAgentToolsDependencies
from .tools.get_entities_by_shared_ids import get_entities_by_shared_ids
from .tools.get_entities_by_template import get_entities_by_template
from .tools.get_entity_store_status import get_entity_store_status
from .tools.get_pages_by_shared_ids import get_pages_by_shared_ids
from .tools.get_relationship_type_names import get_relationship_type_names
from .tools.get_template_names import get_template_names
from .tools.get_templates_by_names import get_templates_by_names
from .tools.get_thesauris_by_names import get_thesauris_by_names
from .tools.get_thesauris_names import get_thesauris_names
from .tools.get_languages import get_languages
from .tools.list_pages import list_pages
from .tools.python_code_executor import run_python_code
from .tools.search_entities_by_filter import search_entities_by_filter
from .tools.search_entities_by_text import search_entities_by_text
from .tools.set_entities_publish_status import set_entities_publish_status
from .tools.update_entities import update_entities
from .tools.update_pages import update_pages
from .tools.update_relationship_type import update_relationship_type
from .tools.update_template import update_template
from .tools.update_thesauri import update_thesauri


_TEMPLATE_READ_TOOLS = {"get_template_names", "get_templates_by_names"}
_THESAURI_READ_TOOLS = {"get_thesauris_names", "get_thesauris_by_names"}
_RELATIONSHIP_READ_TOOLS = {"get_relationship_type_names"}
_ENTITY_READ_TOOLS = {
    "search_entities_by_text",
    "search_entities_by_filter",
    "get_entities_by_shared_ids",
    "get_entities_by_template",
}
_PAGE_READ_TOOLS = {"list_pages", "get_pages_by_shared_ids"}
_LANGUAGE_READ_TOOLS = {"get_languages"}

_WRITE_INVALIDATION_MAP: dict[str, tuple[set[str], Callable | None]] = {
    "create_template": (
        _TEMPLATE_READ_TOOLS,
        None,
    ),
    "update_template": (
        _TEMPLATE_READ_TOOLS,
        lambda deps: deps.schema_store.clear_templates(),
    ),
    "delete_template": (
        _TEMPLATE_READ_TOOLS,
        lambda deps: deps.schema_store.clear_templates(),
    ),
    "create_thesauri": (
        _THESAURI_READ_TOOLS,
        lambda deps: deps.schema_store.clear_thesauri(),
    ),
    "update_thesauri": (
        _THESAURI_READ_TOOLS,
        lambda deps: deps.schema_store.clear_thesauri(),
    ),
    "delete_thesauri": (
        _THESAURI_READ_TOOLS,
        lambda deps: deps.schema_store.clear_thesauri(),
    ),
    "create_relationship_type": (_RELATIONSHIP_READ_TOOLS, None),
    "update_relationship_type": (_RELATIONSHIP_READ_TOOLS, None),
    "delete_relationship_type": (_RELATIONSHIP_READ_TOOLS, None),
    "create_entities": (_ENTITY_READ_TOOLS, None),
    "update_entities": (_ENTITY_READ_TOOLS, None),
    "delete_entities_by_shared_ids": (_ENTITY_READ_TOOLS, None),
    "set_entities_publish_status": (_ENTITY_READ_TOOLS, None),
    "create_pages": (_PAGE_READ_TOOLS, None),
    "update_pages": (_PAGE_READ_TOOLS, None),
    "delete_pages_by_shared_ids": (_PAGE_READ_TOOLS, None),
    "run_python_code": (_ENTITY_READ_TOOLS, None),
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


def _wrap_read_tool(func: Callable) -> Callable:
    @functools.wraps(func)
    async def wrapper(ctx: RunContext[UwaziAgentToolsDependencies], *args: Any, **kwargs: Any) -> Any:
        agent_name = get_current_agent()
        params = _extract_params(args, kwargs, func)
        cached = ctx.deps.tool_cache.get(func.__name__, params)
        if cached is not None:
            logger.info("[{}] CACHE HIT: {}({})", agent_name, func.__name__, params)
            return cached
        logger.info("[{}] CALLING: {}({})", agent_name, func.__name__, params)
        ctx.deps.tool_progress.append(_format_progress_msg(agent_name, func.__name__, params))
        result = await func(ctx, *args, **kwargs)
        if not isinstance(result, str) or not result.startswith("Error"):
            ctx.deps.tool_cache.set(func.__name__, params, result)
        return result

    return wrapper


def _wrap_write_tool(func: Callable) -> Callable:
    tool_name = func.__name__
    invalidated_tools, schema_invalidator = _WRITE_INVALIDATION_MAP.get(tool_name, (set(), None))

    @functools.wraps(func)
    async def wrapper(ctx: RunContext[UwaziAgentToolsDependencies], *args: Any, **kwargs: Any) -> Any:
        agent_name = get_current_agent()
        params = _extract_params(args, kwargs, func)
        logger.info("[{}] CALLING: {}({})", agent_name, tool_name, params)
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
        return result

    return wrapper


def _read_tool(func: Callable) -> Tool:
    return Tool(_wrap_read_tool(func), takes_ctx=True)


def _write_tool(func: Callable) -> Tool:
    return Tool(_wrap_write_tool(func), takes_ctx=True)


def build_templates_tools() -> list[Tool]:
    return [
        _read_tool(get_languages),
        _read_tool(get_thesauris_by_names),
        _read_tool(get_thesauris_names),
        _write_tool(create_thesauri),
        _write_tool(update_thesauri),
        _write_tool(delete_thesauri),
        _read_tool(get_relationship_type_names),
        _write_tool(create_relationship_type),
        _write_tool(update_relationship_type),
        _write_tool(delete_relationship_type),
        _read_tool(get_templates_by_names),
        _read_tool(get_template_names),
        _write_tool(create_template),
        _write_tool(update_template),
        _write_tool(delete_template),
    ]


def build_entity_tools() -> list[Tool]:
    return [
        _read_tool(get_languages),
        _read_tool(search_entities_by_text),
        _read_tool(search_entities_by_filter),
        _read_tool(get_entities_by_shared_ids),
        _read_tool(get_entities_by_template),
        _write_tool(create_entities),
        _write_tool(update_entities),
        _write_tool(set_entities_publish_status),
        _write_tool(delete_entities_by_shared_ids),
    ]


def build_page_tools() -> list[Tool]:
    return [
        _read_tool(list_pages),
        _read_tool(get_pages_by_shared_ids),
        _write_tool(create_pages),
        _write_tool(update_pages),
        _write_tool(delete_pages_by_shared_ids),
    ]


def build_python_tools() -> list[Tool]:
    return [
        _write_tool(run_python_code),
        _read_tool(search_entities_by_text),
        _read_tool(search_entities_by_filter),
        _read_tool(get_entities_by_template),
        _read_tool(get_entities_by_shared_ids),
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
        instructions=PYTHON_INSTRUCTIONS,
        tools=build_python_tools(),
    )


def _make_delegation_tool(
    sub_agent: Agent[UwaziAgentToolsDependencies, str],
    name: str,
    description: str,
) -> Tool:
    agent_label = name.replace("delegate_to_", "")

    async def delegate(ctx: RunContext[UwaziAgentToolsDependencies], task: str) -> str:
        parent_agent = get_current_agent()
        logger.info("[{}] DELEGATING to {} (task: {}...)", parent_agent, agent_label, task[:100])
        ctx.deps.tool_progress.append(f"Delegating to {agent_label} agent...")
        set_current_agent(agent_label)
        try:
            schema_context = ctx.deps.schema_store.to_prompt_context()
            enriched_task = f"{schema_context}\n\n{task}" if schema_context else task
            result = await sub_agent.run(enriched_task, deps=ctx.deps, usage=ctx.usage)
            logger.info("[{}] DELEGATION COMPLETE", agent_label)
            return result.output
        except UsageLimitExceeded as exc:
            logger.error("[{}] DELEGATION BUDGET EXHAUSTED: {}", agent_label, exc)
            return (
                f"Sub-agent budget exhausted ({exc}). "
                f"The task was too complex or the agent entered an error loop. "
                f"Try breaking it into smaller steps and retrying."
            )
        except Exception as exc:
            logger.error("[{}] DELEGATION FAILED: {}", agent_label, exc)
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
            "Delegate page mutation tasks (create, update, delete pages) to the page "
            "sub-agent. Do NOT use this for reading pages — use the read tools directly instead.",
        ),
        _make_delegation_tool(
            python_agent,
            "delegate_to_python_agent",
            "Delegate batch processing of large entity sets to the Python sub-agent. "
            "Use this when the entity agent reports that entities exceed the LLM limit of 5. "
            "The Python agent is the ONLY agent allowed to process more than 5 entities. "
            "It generates and executes Python code with CRUD capabilities "
            "on entities stored in the session entity store.",
        ),
    ]

    read_tools = [
        _read_tool(get_template_names),
        _read_tool(get_templates_by_names),
        _read_tool(get_thesauris_names),
        _read_tool(get_thesauris_by_names),
        _read_tool(get_relationship_type_names),
        _read_tool(get_languages),
        _read_tool(search_entities_by_text),
        _read_tool(search_entities_by_filter),
        _read_tool(get_entities_by_shared_ids),
        _read_tool(get_entities_by_template),
        _read_tool(list_pages),
        _read_tool(get_pages_by_shared_ids),
        Tool(get_entity_store_status, takes_ctx=True),
    ]

    all_tools = delegation_tools + read_tools

    return Agent(
        model,
        deps_type=UwaziAgentToolsDependencies,
        instructions=ORCHESTRATOR_INSTRUCTIONS,
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
