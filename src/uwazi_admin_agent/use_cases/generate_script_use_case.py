"""Turn a ``RunConfig.prompt`` into a :class:`GeneratedScript` (§2.1, Phase 2).

A thin pydantic-ai ``Agent`` use case mirroring ``browser_agent``'s
``GenerateZendriverScriptUseCase``: the agent has ``query_entities`` (imported
from ``uwazi_agent``) so the LLM can explore the instance before writing the
script, and ``run_validation_script`` (a Phase-2 stub; the dummy-entity harness
lands in Phase 3). The structured ``GeneratedScript`` is the result type, and
:meth:`repair` reuses the prior message history so a lint/repair loop (driven by
the CLI in Phase 5) can fix the script without re-discovering.

No browser session to tear down (unlike ``browser_agent``), so there is no
``close``. The model comes from the injected :class:`LlmPort` (reused from
``uwazi_agent``); the live :class:`UwaziAgentToolsDependencies` (entity API,
entity store, ...) is stored on the use case so ``execute`` and ``repair`` share
one entity store for the run.

Not unit-tested (needs an LLM); validated by import + agent construction with a
``TestModel`` and later by the simulation harness.
"""

from __future__ import annotations

from typing import Any

from loguru import logger
from pydantic_ai import Agent, UsageLimits
from pydantic_ai.models import Model

from uwazi_admin_agent.configuration import MAX_LLM_CALLS
from uwazi_admin_agent.domain.generated_script import GeneratedScript
from uwazi_admin_agent.use_cases.run_validation_script_tool import run_validation_script
from uwazi_admin_agent.use_cases.system_prompt import SYSTEM_PROMPT
from uwazi_agent.ports.llm_port import LlmPort
from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies
from uwazi_agent.use_cases.tools.query_entities import query_entities


class GenerateScriptUseCase:
    """Build the script-generation agent, run it once, return a :class:`GeneratedScript`."""

    def __init__(self, llm: LlmPort, deps: UwaziAgentToolsDependencies) -> None:
        self._llm: LlmPort = llm
        self._deps: UwaziAgentToolsDependencies = deps
        self._last_messages: list[Any] = []

    @staticmethod
    def _build_agent(model: Model) -> Agent[UwaziAgentToolsDependencies, GeneratedScript]:
        """Construct the pydantic-ai agent (no deps/llm instance needed)."""
        return Agent(
            model,
            system_prompt=SYSTEM_PROMPT,
            deps_type=UwaziAgentToolsDependencies,
            output_type=GeneratedScript,
            tools=[query_entities, run_validation_script],
        )

    async def execute(self, prompt: str) -> GeneratedScript:
        """Run the agent once on ``prompt`` and return the generated script."""
        agent = self._build_agent(self._llm.get_model())
        run = await agent.run(
            prompt,
            deps=self._deps,
            usage_limits=UsageLimits(request_limit=MAX_LLM_CALLS),
        )
        self._last_messages = list(run.all_messages())
        script = self._coerce_result(run)
        logger.info("generated script: lines={} preview={}", script.python_code.count("\n") + 1, script.python_code[:200])
        return script

    async def repair(self, feedback: str) -> GeneratedScript:
        """Run a repair turn with ``feedback`` over the prior message history.

        Reuses the discovery + generation context from the prior ``execute`` so
        the agent can fix the script without re-exploring. Driven by the CLI's
        repair loop (Phase 5).
        """
        agent = self._build_agent(self._llm.get_model())
        run = await agent.run(
            feedback,
            deps=self._deps,
            usage_limits=UsageLimits(request_limit=MAX_LLM_CALLS),
            message_history=self._last_messages,
        )
        self._last_messages = list(run.all_messages())
        script = self._coerce_result(run)
        logger.info("repaired script: lines={} preview={}", script.python_code.count("\n") + 1, script.python_code[:200])
        return script

    @staticmethod
    def _coerce_result(run: Any) -> GeneratedScript:
        output = getattr(run, "output", None)
        if isinstance(output, GeneratedScript):
            return output
        raise RuntimeError(f"Agent returned an unsupported output type: {type(output).__name__}")
