"""pydantic-ai planner enforcing §2.1 (emit only a MigrationPlan, never code).

The pydantic-ai ``Model`` is injected so this adapter is provider-agnostic and
importable without an API key. The model/provider is chosen by the driver (Phase 6);
per §7 the planner is configured independently of ``uwazi_agent``.
"""

from typing import override

from pydantic_ai import Agent
from pydantic_ai.models import Model

from uwazi_admin_agent.domain.plan import MigrationPlan
from uwazi_admin_agent.ports.planner_port import PlannerPort

PLANNER_SYSTEM_PROMPT = """You are the planner for a Uwazi admin migration agent.

Your ONLY job: turn the operator's natural-language prompt into a declarative
MigrationPlan. Emit the structured MigrationPlan and nothing else - never code,
never free-form instructions, never prose outside the schema.

Hard rules:
- Use only the declared op kinds: "set_property", "extract_from_supporting_file",
  "restructure_languages". Never invent a new kind; a new capability is a new op
  kind added by the engineers, not by you.
- Every op's filter must select at least one entity (shared_ids, template,
  search_text, or language). An empty filter is invalid.
- Property-write ops (set_property, extract_from_supporting_file) carry
  allow_overwrite, default false. Overwrites are never silent.
- The plan must contain at least one op.
- No two property writes to the same (filter, property_name).
- An extraction must say HOW (a css selector or a regex), never "figure it out".
- Never emit a "run code" op. The plan is data, not a program.

If the prompt is ambiguous, pick the most conservative interpretation that still
satisfies the request, and set allow_overwrite=false.
"""


class PydanticAiPlanner(PlannerPort):
    """PlannerPort implementation backed by a pydantic-ai Agent."""

    def __init__(self, model: Model) -> None:
        self._model: Model = model

    @override
    async def plan(self, prompt: str) -> MigrationPlan:
        agent = Agent(self._model, output_type=MigrationPlan, instructions=PLANNER_SYSTEM_PROMPT)
        result = await agent.run(prompt)
        return result.output
