"""Turn a ``RunConfig.prompt`` into a :class:`GeneratedScript`.

A thin pydantic-ai ``Agent`` use case: the agent plans the bulk change with
read/inspect tools (schema inspection, entity discovery sampling, HTML/URL
peeking, segmentation) and emits ONE deterministic script whose recorded
writes become the review table. The script is NEVER executed against Uwazi
during generation (no dummy-validation gate, no dry run) — the operator's
review table IS the proof step.

The generated script MAY call ``llm_complete``/``llm_map``; the LLM caps the
operator approves in the wizard (a request cap + a dollar cap) are enforced on
THOSE calls by the :class:`LlmLedger` bound into the script's namespace when
the planning pass runs — never on this generation conversation, which keeps
the built-in ``MAX_LLM_CALLS`` pydantic-ai cap.

The model comes from the injected :class:`LlmPort` (reused from
``uwazi_agent``); the live :class:`AdminAgentDeps` (entity API, entity store,
...) is stored on the use case. The system prompt instructs the generator to
avoid the LLM tools in the script whenever pure computation suffices, so the
caps are a ceiling, not a quota. The instance's configured language keys are
appended to that prompt at run time: the script must only write languages the
instance actually has, never an invented candidate list.

Not unit-tested (needs an LLM); validated by import + agent construction with a
``TestModel`` and by the review-table flow.
"""

from __future__ import annotations

from typing import Any

from loguru import logger
from pydantic_ai import Agent, UsageLimits
from pydantic_ai.models import Model

from uwazi_admin_agent.configuration import LLM_MAX_OUTPUT_TOKENS, MAX_LLM_CALLS
from uwazi_admin_agent.domain.generated_script import GeneratedScript
from uwazi_admin_agent.use_cases.admin_agent_deps import AdminAgentDeps
from uwazi_admin_agent.use_cases.author_extractor_tool import author_html_extractor, build_extractor_agent
from uwazi_admin_agent.use_cases.instance_languages import fetch_instance_languages, system_prompt_with_languages
from uwazi_admin_agent.use_cases.peek_file_tools import peek_entity_files, peek_file_text
from uwazi_admin_agent.use_cases.segmentation_tools import peek_segmentation, peek_segmentation_by_entity
from uwazi_admin_agent.use_cases.system_prompt import SYSTEM_PROMPT
from uwazi_admin_agent.use_cases.url_fetch_tools import peek_url_text
from uwazi_agent.ports.llm_port import LlmPort
from uwazi_agent.use_cases.tools.get_template_names import list_templates
from uwazi_agent.use_cases.tools.get_templates_by_names import get_templates_by_names
from uwazi_agent.use_cases.tools.get_thesauris_by_names import get_thesauris_by_names
from uwazi_agent.use_cases.tools.get_thesauris_names import list_thesauri
from uwazi_agent.use_cases.tools.query_entities import query_entities


class GenerateScriptUseCase:
    """Build the script-generation agent, run it once, return a :class:`GeneratedScript`."""

    def __init__(
        self,
        llm: LlmPort,
        deps: AdminAgentDeps,
        max_requests: int = MAX_LLM_CALLS,
        max_output_tokens: int = LLM_MAX_OUTPUT_TOKENS,
    ) -> None:
        self._llm: LlmPort = llm
        # The operator-approved ceiling from the wizard's caps step: the
        # request cap pydantic-ai enforces for every turn, and the per-call
        # output budget (the GENERATION conversation's caps, not the script's).
        self._max_requests: int = max(1, max_requests)
        self._max_output_tokens: int = max(1, max_output_tokens)
        self._deps: AdminAgentDeps = deps
        self._last_messages: list[Any] = []
        # The same conversation serialized (``all_messages_json()``), written to the
        # run folder so a later repair turn can restore the full context.
        self._last_messages_json: bytes | None = None
        # Build the extractor subagent ONCE (the use case owns the LlmPort) and
        # share it with the author_html_extractor tool via the deps.
        deps.extractor_agent = build_extractor_agent(self._llm.get_model())

    @staticmethod
    def _build_agent(
        model: Model,
        max_output_tokens: int = LLM_MAX_OUTPUT_TOKENS,
        system_prompt: str = SYSTEM_PROMPT,
    ) -> Agent[AdminAgentDeps, GeneratedScript]:
        """Construct the pydantic-ai agent (no deps/llm instance needed).

        ``system_prompt`` defaults to the static contract; :meth:`execute` and
        :meth:`repair` pass the contract plus the instance's configured
        languages so the script never invents language keys.

        ``model_settings`` raises the per-call OUTPUT token budget above the
        provider default so a long script + the ``GeneratedScript`` JSON
        wrapping + reasoning fits; without it pydantic-ai aborts the final emit
        with ``UnexpectedModelBehavior`` (intermittent — short scripts fit the
        default). Scoped here rather than on the shared ``OllamaAdapter`` so
        only this package's generation is affected. Independent of
        ``UsageLimits(request_limit=...)`` (a request count, not tokens); both
        come from the operator-approved generation budget (the dollar cap
        approved in the wizard is for the SCRIPT's LLM tool calls, not here).
        """
        return Agent(
            model,
            system_prompt=system_prompt,
            deps_type=AdminAgentDeps,
            output_type=GeneratedScript,
            tools=[
                # Discovery sampling: entities (for the script's target set).
                query_entities,
                # Schema inspection: without these the LLM cannot learn a
                # template's property names/types/required-fields or thesaurus
                # labels, so it GUESSES (e.g. a `summary` property that does
                # not exist). These read-only tools are reused from
                # `uwazi_agent` (plan §3: import, not copy). All deps they
                # touch (template_api, thesauri_api, template_mapper,
                # schema_store) are wired in `drivers/runtime.py`.
                get_templates_by_names,
                list_templates,
                get_thesauris_by_names,
                list_thesauri,
                # HTML extraction: sampling peek tools + the nested extractor author.
                author_html_extractor,
                peek_entity_files,
                peek_file_text,
                # Document segmentation: read the structured text of a primary
                # document (paragraphs, page-ordered) for extraction over docs.
                peek_segmentation,
                peek_segmentation_by_entity,
                # External URL fetch: read a referenced page (an entity's Source
                # Page URL / URL attachment) when there is no uploaded file.
                peek_url_text,
            ],
            model_settings={"max_tokens": max_output_tokens},
        )

    async def _system_prompt(self) -> str:
        """The contract PLUS this instance's configured language keys.

        The generator writes ``language=`` arguments, so it must know the real
        set. Without it the model invents candidate lists of ISO codes and the
        script plans writes into languages the instance does not have. The
        fetch is best-effort: on failure the prompt says the list is
        unavailable and forbids inventing one.
        """
        return system_prompt_with_languages(SYSTEM_PROMPT, await fetch_instance_languages(self._deps))

    async def execute(self, prompt: str) -> GeneratedScript:
        """Run the agent once on ``prompt`` and return the generated script."""
        agent = self._build_agent(self._llm.get_model(), self._max_output_tokens, await self._system_prompt())
        run = await agent.run(
            prompt,
            deps=self._deps,
            usage_limits=UsageLimits(request_limit=self._max_requests),
        )
        self._last_messages = list(run.all_messages())
        self._last_messages_json = run.all_messages_json()
        script = self._coerce_result(run)
        logger.info("generated script: lines={} preview={}", script.python_code.count("\n") + 1, script.python_code[:200])
        return script

    def load_history_json(self, payload: bytes | str) -> None:
        """Restore the conversation from a persisted ``all_messages_json()`` payload.

        The repair path reloads a run's stored history so a repair turn can fix
        the script with the ORIGINAL discovery context still in view (no
        re-exploration, no re-discovery cost).
        """
        from pydantic_ai.messages import ModelMessagesTypeAdapter

        raw = payload.encode("utf-8") if isinstance(payload, str) else payload
        self._last_messages = list(ModelMessagesTypeAdapter.validate_json(raw))

    async def repair(self, feedback: str) -> GeneratedScript:
        """Run a repair turn with ``feedback`` over the prior message history.

        Reuses the discovery + generation context from the prior ``execute`` so
        the agent can fix the script without re-exploring.
        """
        agent = self._build_agent(self._llm.get_model(), self._max_output_tokens, await self._system_prompt())
        run = await agent.run(
            feedback,
            deps=self._deps,
            usage_limits=UsageLimits(request_limit=self._max_requests),
            message_history=self._last_messages,
        )
        self._last_messages = list(run.all_messages())
        self._last_messages_json = run.all_messages_json()
        script = self._coerce_result(run)
        logger.info("repaired script: lines={} preview={}", script.python_code.count("\n") + 1, script.python_code[:200])
        return script

    @staticmethod
    def _coerce_result(run: Any) -> GeneratedScript:
        output = getattr(run, "output", None)
        if isinstance(output, GeneratedScript):
            return output
        raise RuntimeError(f"Agent returned an unsupported output type: {type(output).__name__}")
