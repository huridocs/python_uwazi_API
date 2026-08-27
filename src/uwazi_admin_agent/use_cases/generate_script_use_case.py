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

from uwazi_admin_agent.configuration import LLM_MAX_OUTPUT_TOKENS, MAX_LLM_CALLS
from uwazi_admin_agent.domain.generated_script import GeneratedScript
from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort
from uwazi_admin_agent.use_cases.admin_agent_deps import AdminAgentDeps
from uwazi_admin_agent.use_cases.author_extractor_tool import (
    author_html_extractor,
    build_extractor_agent,
)
from uwazi_admin_agent.use_cases.peek_file_tools import peek_entity_files, peek_file_text
from uwazi_admin_agent.use_cases.run_dry_run_script_tool import run_dry_run_script
from uwazi_admin_agent.use_cases.run_validation_script_tool import run_validation_script
from uwazi_admin_agent.use_cases.system_prompt import SYSTEM_PROMPT
from uwazi_agent.ports.llm_port import LlmPort
from uwazi_agent.use_cases.tools.get_template_names import list_templates
from uwazi_agent.use_cases.tools.get_templates_by_names import get_templates_by_names
from uwazi_agent.use_cases.tools.get_thesauris_by_names import get_thesauris_by_names
from uwazi_agent.use_cases.tools.get_thesauris_names import list_thesauri
from uwazi_agent.use_cases.tools.query_entities import query_entities


class GenerateScriptUseCase:
    """Build the script-generation agent, run it once, return a :class:`GeneratedScript`."""

    def __init__(self, llm: LlmPort, deps: AdminAgentDeps, entity_repository: EntityRepositoryPort) -> None:
        self._llm: LlmPort = llm
        self._deps: AdminAgentDeps = deps
        self._entity_repository: EntityRepositoryPort = entity_repository
        self._last_messages: list[Any] = []
        # Wire the raw repository onto the deps so the validation tool can snapshot/revert.
        deps.entity_repository = entity_repository
        # Build the extractor subagent ONCE (the use case owns the LlmPort) and
        # share it with the author_html_extractor tool via the deps.
        deps.extractor_agent = build_extractor_agent(self._llm.get_model())

    @staticmethod
    def _build_agent(model: Model) -> Agent[AdminAgentDeps, GeneratedScript]:
        """Construct the pydantic-ai agent (no deps/llm instance needed).

        ``model_settings`` raises the per-call OUTPUT token budget above the
        provider default so a long script (multi-group merge) + the
        ``GeneratedScript`` JSON wrapping + reasoning fits; without it pydantic-ai
        aborts the final emit with ``UnexpectedModelBehavior`` (intermittent -
        short scripts fit the default). Scoped here rather than on the shared
        ``OllamaAdapter`` so only this package's generation is affected. Independent
        of ``UsageLimits(request_limit=MAX_LLM_CALLS)`` (a request count, not tokens).
        """
        return Agent(
            model,
            system_prompt=SYSTEM_PROMPT,
            deps_type=AdminAgentDeps,
            output_type=GeneratedScript,
            tools=[
                # Discovery: entities (for the script's target set).
                query_entities,
                # Schema inspection (Phase-8 create/fix): without these the LLM
                # cannot learn a template's property names/types/required-fields or
                # thesaurus labels, so it GUESSES (e.g. a `summary` property that
                # does not exist) -> Uwazi rejects the create -> 0 entities. These
                # read-only tools are reused from `uwazi_agent` (plan §3: import,
                # not copy). All deps they touch (template_api, thesauri_api,
                # template_mapper, schema_store) are wired in `drivers/runtime.py`;
                # `stats_api` is None there, which degrades gracefully (no usage
                # counts - the LLM needs names/types/labels, not counts).
                get_templates_by_names,
                list_templates,
                get_thesauris_by_names,
                list_thesauri,
                # The validation gate.
                run_validation_script,
                # The real-data rehearsal: real reads, recorded no-op writes.
                run_dry_run_script,
                # HTML extraction: sampling peek tools + the nested extractor author.
                author_html_extractor,
                peek_entity_files,
                peek_file_text,
            ],
            model_settings={"max_tokens": LLM_MAX_OUTPUT_TOKENS},
        )

    async def execute(self, prompt: str) -> GeneratedScript:
        """Run the agent once on ``prompt`` and return the generated script."""
        self._deps.validation_attempts = 0  # fresh budget per generation turn
        self._deps.dry_run_attempts = 0  # fresh dry-run budget per generation turn
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
        # Note: repair does NOT reset the validation counter — the limit spans the
        # whole generation+repair turn, mirroring ``browser_agent``.
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
