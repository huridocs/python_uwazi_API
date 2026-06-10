from typing import Sequence

from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.tools import Tool

from .tools.create_entities import create_entities
from .tools.create_pages import create_pages
from .tools.create_template import create_template
from .tools.create_thesauri import create_thesauri
from .tools.delete_entities_by_shared_ids import delete_entities_by_shared_ids
from .tools.delete_pages_by_shared_ids import delete_pages_by_shared_ids
from .tools.delete_template import delete_template
from .tools.delete_thesauri import delete_thesauri
from .tools.get_entities_by_shared_ids import get_entities_by_shared_ids
from .tools.get_pages_by_shared_ids import get_pages_by_shared_ids
from .tools.get_template_names import get_template_names
from .tools.get_templates_by_names import get_templates_by_names
from .tools.get_thesauris_by_names import get_thesauris_by_names
from .tools.get_thesauris_names import get_thesauris_names
from .tools.list_pages import list_pages
from .tools.search_entities_by_text import search_entities_by_text
from .tools.update_entities import update_entities
from .tools.update_pages import update_pages
from .tools.update_template import update_template
from .tools.update_thesauri import update_thesauri


def build_thesauri_tools() -> list[Tool]:
    return [
        Tool(get_thesauris_by_names, takes_ctx=True),
        Tool(get_thesauris_names, takes_ctx=True),
        Tool(create_thesauri, takes_ctx=True),
        Tool(update_thesauri, takes_ctx=True),
        Tool(delete_thesauri, takes_ctx=True),
    ]


def build_template_tools() -> list[Tool]:
    return [
        Tool(get_templates_by_names, takes_ctx=True),
        Tool(get_template_names, takes_ctx=True),
        Tool(create_template, takes_ctx=True),
        Tool(update_template, takes_ctx=True),
        Tool(delete_template, takes_ctx=True),
    ]


def build_entity_tools() -> list[Tool]:
    return [
        Tool(search_entities_by_text, takes_ctx=True),
        Tool(get_entities_by_shared_ids, takes_ctx=True),
        Tool(create_entities, takes_ctx=True),
        Tool(update_entities, takes_ctx=True),
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


def build_uwazi_agent(
    model: Model,
    deps_type: type,
    instructions: str | None = None,
    extra_tools: Sequence[Tool] = (),
    include_thesauri: bool = True,
    include_templates: bool = True,
    include_entities: bool = True,
    include_pages: bool = True,
) -> Agent:
    tools: list[Tool] = []
    if include_thesauri:
        tools.extend(build_thesauri_tools())
    if include_templates:
        tools.extend(build_template_tools())
    if include_entities:
        tools.extend(build_entity_tools())
    if include_pages:
        tools.extend(build_page_tools())
    tools.extend(extra_tools)
    return Agent(
        model,
        deps_type=deps_type,
        instructions=instructions,
        tools=tools,
    )
