from dataclasses import dataclass

from loguru import logger
from pydantic_ai.usage import RunUsage

from uwazi_agent.ports.entity_api_port import EntityApiPort
from uwazi_agent.ports.llm_port import LlmPort
from uwazi_agent.ports.page_api_port import PageApiPort
from uwazi_agent.ports.relationship_type_api_port import RelationshipTypeApiPort
from uwazi_agent.ports.settings_api_port import SettingsApiPort
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
        settings_api: SettingsApiPort | None = None,
    ):
        self.llm = llm
        self.thesauri_api = thesauri_api
        self.template_api = template_api
        self.template_mapper = template_mapper
        self.entity_api = entity_api
        self.page_api = page_api
        self.relationship_type_api = relationship_type_api
        self.settings_api = settings_api

    async def execute(self, task_description: str, context: str = "") -> AgentExecutionResult:
        prompt = self._compose_prompt(task_description=task_description, context=context)
        logger.info("PROMPT: {}", prompt)
        deps = UwaziAgentToolsDependencies(
            thesauri_api=self.thesauri_api,
            template_api=self.template_api,
            template_mapper=self.template_mapper,
            relationship_type_api=self.relationship_type_api,
            entity_api=self.entity_api,
            page_api=self.page_api,
            settings_api=self.settings_api,
        )
        agent = build_uwazi_agents(
            model=self.llm.get_model(),
            include_entities=self.entity_api is not None,
            include_pages=self.page_api is not None,
        )
        try:
            result = await agent.run(prompt, deps=deps)
            logger.info("PROMPT SUCCEEDED: {}", task_description[:200])
            return AgentExecutionResult(
                output=result.output,
                thinking=result.response.thinking,
                usage=result.usage,
            )
        except Exception as exc:
            logger.error("PROMPT FAILED: {} | error: {}", task_description[:200], exc)
            raise

    @staticmethod
    def _compose_prompt(task_description: str, context: str) -> str:
        context = (context or "").strip()
        task = (task_description or "").strip()
        if context:
            return f"Context:\n{context}\n\nTask:\n{task}"
        return task
