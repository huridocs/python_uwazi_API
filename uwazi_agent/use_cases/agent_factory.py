from pydantic_ai import Agent, RunContext
from pydantic_ai.models import Model
from pydantic_ai.tools import Tool

from .instructions import (
    ENTITY_INSTRUCTIONS,
    ORCHESTRATOR_INSTRUCTIONS,
    PAGE_INSTRUCTIONS,
    PYTHON_INSTRUCTIONS,
    TEMPLATES_INSTRUCTIONS,
)
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


def build_templates_tools() -> list[Tool]:
    return [
        Tool(get_thesauris_by_names, takes_ctx=True),
        Tool(get_thesauris_names, takes_ctx=True),
        Tool(create_thesauri, takes_ctx=True),
        Tool(update_thesauri, takes_ctx=True),
        Tool(delete_thesauri, takes_ctx=True),
        Tool(get_relationship_type_names, takes_ctx=True),
        Tool(create_relationship_type, takes_ctx=True),
        Tool(update_relationship_type, takes_ctx=True),
        Tool(delete_relationship_type, takes_ctx=True),
        Tool(get_templates_by_names, takes_ctx=True),
        Tool(get_template_names, takes_ctx=True),
        Tool(create_template, takes_ctx=True),
        Tool(update_template, takes_ctx=True),
        Tool(delete_template, takes_ctx=True),
    ]


def build_entity_tools() -> list[Tool]:
    return [
        Tool(search_entities_by_text, takes_ctx=True),
        Tool(search_entities_by_filter, takes_ctx=True),
        Tool(get_entities_by_shared_ids, takes_ctx=True),
        Tool(get_entities_by_template, takes_ctx=True),
        Tool(create_entities, takes_ctx=True),
        Tool(update_entities, takes_ctx=True),
        Tool(set_entities_publish_status, takes_ctx=True),
        Tool(delete_entities_by_shared_ids, takes_ctx=True),
    ]


def build_page_tools() -> list[Tool]:
    return [
        Tool(list_pages, takes_ctx=True),
        Tool(get_pages_by_shared_ids, takes_ctx=True),
        Tool(create_pages, takes_ctx=True),
        Tool(update_pages, takes_ctx=True),
        Tool(delete_pages_by_shared_ids, takes_ctx=True),
    ]


def build_python_tools() -> list[Tool]:
    return [
        Tool(run_python_code, takes_ctx=True),
        Tool(search_entities_by_text, takes_ctx=True),
        Tool(search_entities_by_filter, takes_ctx=True),
        Tool(get_entities_by_template, takes_ctx=True),
        Tool(get_entities_by_shared_ids, takes_ctx=True),
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
    async def delegate(ctx: RunContext[UwaziAgentToolsDependencies], task: str) -> str:
        try:
            schema_context = ctx.deps.schema_store.to_prompt_context()
            enriched_task = f"{schema_context}\n\n{task}" if schema_context else task
            result = await sub_agent.run(enriched_task, deps=ctx.deps, usage=ctx.usage)
            return result.output
        except Exception as exc:
            return f"Sub-agent error ({name}): {exc}. Please rephrase the task or break it into smaller steps and retry."

    delegate.__name__ = name
    delegate.__qualname__ = name
    delegate.__doc__ = description
    return Tool(delegate, takes_ctx=True, name=name, description=description)


def build_orchestrator(
    model: Model,
    schema_agent: Agent[UwaziAgentToolsDependencies, str],
    entity_agent: Agent[UwaziAgentToolsDependencies, str] | None = None,
    page_agent: Agent[UwaziAgentToolsDependencies, str] | None = None,
    python_agent: Agent[UwaziAgentToolsDependencies, str] | None = None,
) -> Agent[UwaziAgentToolsDependencies, str]:
    delegation_tools = [
        _make_delegation_tool(
            schema_agent,
            "delegate_to_schema_agent",
            "Delegate schema-related tasks (thesauri and templates) to the schema sub-agent.",
        ),
    ]
    if entity_agent is not None:
        delegation_tools.append(
            _make_delegation_tool(
                entity_agent,
                "delegate_to_entity_agent",
                "Delegate entity tasks (search, create, update, delete) to the entity sub-agent.",
            )
        )
    if page_agent is not None:
        delegation_tools.append(
            _make_delegation_tool(
                page_agent,
                "delegate_to_page_agent",
                "Delegate page tasks (list, create, update, delete) to the page sub-agent.",
            )
        )
    if python_agent is not None:
        delegation_tools.append(
            _make_delegation_tool(
                python_agent,
                "delegate_to_python_agent",
                "Delegate batch processing of large entity sets to the Python sub-agent. "
                "Use this when the entity agent reports that entities exceed the LLM limit of 5. "
                "The Python agent is the ONLY agent allowed to process more than 5 entities. "
                "It generates and executes Python code with CRUD capabilities "
                "on entities stored in the session entity store.",
            )
        )

    delegation_tools.append(
        Tool(get_entity_store_status, takes_ctx=True),
    )

    return Agent(
        model,
        deps_type=UwaziAgentToolsDependencies,
        instructions=ORCHESTRATOR_INSTRUCTIONS,
        tools=delegation_tools,
    )


def build_uwazi_agents(
    model: Model,
    include_entities: bool = True,
    include_pages: bool = True,
) -> Agent[UwaziAgentToolsDependencies, str]:
    schema_agent = build_templates_agent(model)

    if not include_entities and not include_pages:
        return schema_agent

    entity_agent = build_entity_agent(model) if include_entities else None
    page_agent = build_page_agent(model) if include_pages else None
    python_agent = build_python_agent(model) if include_entities else None

    return build_orchestrator(
        model=model,
        schema_agent=schema_agent,
        entity_agent=entity_agent,
        page_agent=page_agent,
        python_agent=python_agent,
    )
