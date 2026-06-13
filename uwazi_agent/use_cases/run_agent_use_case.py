from dataclasses import dataclass
from pathlib import Path

from loguru import logger
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.usage import RunUsage, UsageLimits

from uwazi_agent.configuration import REQUEST_LIMIT
from uwazi_agent.ports.entity_api_port import EntityApiPort
from uwazi_agent.ports.llm_port import LlmPort
from uwazi_agent.ports.page_api_port import PageApiPort
from uwazi_agent.ports.relationship_api_port import RelationshipApiPort
from uwazi_agent.ports.relationship_type_api_port import RelationshipTypeApiPort
from uwazi_agent.ports.settings_api_port import SettingsApiPort
from uwazi_agent.ports.stats_api_port import StatsApiPort
from uwazi_agent.ports.template_api_port import TemplateApiPort
from uwazi_agent.ports.template_mapper_port import TemplateMapperPort
from uwazi_agent.ports.thesauri_api_port import ThesauriApiPort
from uwazi_agent.use_cases.agent_factory import build_uwazi_agents
from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies


@dataclass
class AgentExecutionResult:
    output: str
    thinking: str | None
    usage: RunUsage


_DEFAULT_PAGE_BUILDER_DIR = Path(__file__).resolve().parent.parent / "drivers" / "page_builder"


class RunAgentUseCase:
    def __init__(
        self,
        llm: LlmPort,
        thesauri_api: ThesauriApiPort,
        template_api: TemplateApiPort,
        template_mapper: TemplateMapperPort,
        entity_api: EntityApiPort,
        page_api: PageApiPort,
        relationship_type_api: RelationshipTypeApiPort | None = None,
        relationship_api: RelationshipApiPort | None = None,
        settings_api: SettingsApiPort | None = None,
        stats_api: StatsApiPort | None = None,
        page_builder_dir: Path | None = None,
    ):
        self.llm = llm
        self.thesauri_api = thesauri_api
        self.template_api = template_api
        self.template_mapper = template_mapper
        self.entity_api = entity_api
        self.page_api = page_api
        self.relationship_type_api = relationship_type_api
        self.relationship_api = relationship_api
        self.settings_api = settings_api
        self.stats_api = stats_api
        self.page_builder_dir = page_builder_dir or _DEFAULT_PAGE_BUILDER_DIR

    async def execute(
        self, task_description: str, context: str = "", tool_progress: list[str] | None = None
    ) -> AgentExecutionResult:
        deps = UwaziAgentToolsDependencies(
            thesauri_api=self.thesauri_api,
            template_api=self.template_api,
            template_mapper=self.template_mapper,
            relationship_type_api=self.relationship_type_api,
            relationship_api=self.relationship_api,
            entity_api=self.entity_api,
            page_api=self.page_api,
            settings_api=self.settings_api,
            stats_api=self.stats_api,
            page_builder_dir=self.page_builder_dir,
        )
        if tool_progress is not None:
            deps.tool_progress = tool_progress
        # Pre-load the four lightweight discovery data sets (languages,
        # templates, thesauri, relationship types) into the schema store
        # so they can be rendered into the user prompt without a tool call.
        # Failures here are non-fatal: the prompt just omits the affected
        # section.
        await self._populate_available_context(deps)
        # Populate the page-builder section of the schema store from the
        # on-disk BlockRegistry / VibeRegistry. This is the only place
        # the LLM ever needs to see the page block library and visual
        # themes — there is no tool for it. The page sub-agent's prompt
        # picks this up via SchemaStore.to_page_prompt_context().
        self._populate_page_builder(deps)
        available_context = deps.schema_store.to_available_context()
        prompt = self._compose_prompt(
            task_description=task_description,
            context=context,
            available_context=available_context,
        )
        logger.info("PROMPT: {}", prompt)
        agent = build_uwazi_agents(model=self.llm.get_model())
        usage_limits = UsageLimits(request_limit=REQUEST_LIMIT)
        try:
            result = await agent.run(prompt, deps=deps, usage_limits=usage_limits)
            logger.info("PROMPT SUCCEEDED: {}", task_description[:200])
            return AgentExecutionResult(
                output=result.output,
                thinking=result.response.thinking,
                usage=result.usage,
            )
        except UsageLimitExceeded as exc:
            logger.error("PROMPT FAILED (request limit): {} | error: {}", task_description[:200], exc)
            return AgentExecutionResult(
                output=(
                    f"The agent exceeded the request limit of {REQUEST_LIMIT} and could not complete the task."
                    f" The task may be too complex or the agent may have entered an error loop."
                    f" Try breaking it into smaller steps."
                ),
                thinking=None,
                usage=RunUsage(),
            )
        except Exception as exc:
            logger.error("PROMPT FAILED: {} | error: {}", task_description[:200], exc)
            raise

    @staticmethod
    async def _populate_available_context(deps: UwaziAgentToolsDependencies) -> None:
        """Pre-load the four data sets the orchestrator no longer exposes
        as tools. Wraps the call in a try/except so a transient Uwazi
        hiccup never aborts the run; missing sections are simply omitted
        from the prompt and a warning is logged.
        """
        from uwazi_agent.use_cases.tools.tool_context import populate_all

        # The populate_all helpers expect a ``RunContext``-shaped object
        # that exposes ``.deps``; we synthesise a thin stand-in so we can
        # call them before the agent has been built.
        class _Ctx:
            pass

        ctx = _Ctx()
        ctx.deps = deps
        try:
            await populate_all(ctx)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001 -- defensive: never let pre-load break the run
            logger.warning("available_context pre-load FAILED: {}", exc)

    @staticmethod
    def _compose_prompt(task_description: str, context: str, available_context: str) -> str:
        context = (context or "").strip()
        task = (task_description or "").strip()
        available_context = (available_context or "").strip()
        parts: list[str] = []
        if available_context:
            parts.append(available_context)
        if context:
            parts.append(f"Context:\n{context}")
        parts.append(f"Task:\n{task}")
        return "\n\n".join(parts)

    def _populate_page_builder(self, deps: UwaziAgentToolsDependencies) -> None:
        """Hydrate the schema store's page-builder section from disk.

        Reads the on-disk ``BlockRegistry`` and ``VibeRegistry`` and
        stores the serialised view on ``deps.schema_store``. Called once
        at the start of :meth:`execute`; idempotent within a session
        (re-running it just re-reads the same files).

        If the page builder is not configured (``page_builder_dir`` is
        ``None``), this is a no-op and the page-builder section of the
        prompt stays empty — the page agent will see no blocks/vibes
        and any attempt to use ``create_page(blocks=...)`` will fail
        with the standard "Page builder is not configured" error.
        """
        if deps.page_builder_dir is None:
            return
        try:
            from uwazi_agent.drivers.page_builder.registry import (
                BlockRegistry,
                VibeRegistry,
            )
            from uwazi_agent.drivers.page_builder.renderer import DEFAULT_VIBE

            blocks_dir = deps.page_builder_dir / "blocks"
            vibes_dir = deps.page_builder_dir / "vibes"
            block_registry = BlockRegistry(blocks_dir)
            vibe_registry = VibeRegistry(vibes_dir)
            deps.schema_store.set_page_builder(
                blocks=block_registry.list_blocks(),
                vibes=vibe_registry.list_vibes(),
                default_vibe=DEFAULT_VIBE,
            )
        except Exception as exc:  # noqa: BLE001 — never fail the run on prompt hydration
            logger.warning(
                "Failed to populate page-builder prompt context: {}. "
                "The page agent will not see the block library; "
                "create_page(blocks=...) will return an error.",
                exc,
            )
