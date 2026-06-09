from typing import Sequence

from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.tools import Tool

from uwazi_agent.use_cases.tools.create_thesauri import create_thesauri
from uwazi_agent.use_cases.tools.delete_thesauri import delete_thesauri
from uwazi_agent.use_cases.tools.get_thesauris_by_names import get_thesauris_by_names
from uwazi_agent.use_cases.tools.get_thesauris_names import get_thesauris_names
from uwazi_agent.use_cases.tools.update_thesauri import update_thesauri


def build_thesauri_tools() -> list[Tool]:
    """Return the thesaurus tools ready to attach to a pydantic-ai Agent.

    Each tool is registered with ``takes_ctx=True`` so it can pull its
    dependencies (api, mapper) from the agent's ``RunContext.deps`` instead
    of relying on closures or globals.
    """
    return [
        Tool(get_thesauris_by_names, takes_ctx=True),
        Tool(get_thesauris_names, takes_ctx=True),
        Tool(create_thesauri, takes_ctx=True),
        Tool(update_thesauri, takes_ctx=True),
        Tool(delete_thesauri, takes_ctx=True),
    ]


def build_thesauri_agent(
    model: Model,
    deps_type: type,
    instructions: str | None = None,
    extra_tools: Sequence[Tool] = (),
) -> Agent:
    """Build a pydantic-ai Agent pre-loaded with all thesaurus tools.

    The agent's ``deps_type`` must match the type passed to ``agent.run`` so
    pydantic-ai can inject it into the tool's ``RunContext.deps``.
    """
    tools: list[Tool] = list(build_thesauri_tools())
    tools.extend(extra_tools)
    return Agent(
        model,
        deps_type=deps_type,
        instructions=instructions,
        tools=tools,
    )
