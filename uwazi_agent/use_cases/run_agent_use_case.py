from pathlib import Path

from dotenv import load_dotenv

from uwazi_agent.ports.llm_port import LlmDeps, LlmPort
from uwazi_agent.ports.mapper_port import ThesauriMapperPort
from uwazi_agent.ports.uwazi_api_port import ThesauriApiPort


PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")


class RunAgentUseCase:
    def __init__(
        self,
        llm: LlmPort,
        api: ThesauriApiPort,
        mapper: ThesauriMapperPort | None = None,
    ):
        self.llm = llm
        self.api = api
        self.mapper = mapper

    async def execute(self, task_description: str, context: str = "") -> str:
        prompt = self._compose_prompt(task_description=task_description, context=context)
        deps = LlmDeps(api=self.api, mapper=self.mapper)
        return await self.llm.run(prompt, deps=deps)

    @staticmethod
    def _compose_prompt(task_description: str, context: str) -> str:
        context = (context or "").strip()
        task = (task_description or "").strip()
        if context:
            return f"Context:\n{context}\n\nTask:\n{task}"
        return task
