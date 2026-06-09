import os

from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from uwazi_agent.adapters.llm.agent_factory import build_thesauri_agent
from uwazi_agent.configuration import MODEL
from uwazi_agent.ports.llm_port import LlmDeps, LlmPort
from uwazi_agent.use_cases.tools.dependencies import ThesauriToolsDependencies


DEFAULT_INSTRUCTIONS = (
    "You are an assistant that manages thesauri (controlled vocabularies) in a "
    "Uwazi instance. Use the provided tools to look up, create, update, or "
    "delete thesauri by their human-readable name. Always identify a thesaurus "
    "by its name, never by an internal id. Confirm the result of any mutation "
    "back to the user in plain language."
)

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterAdapter(LlmPort):
    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        instructions: str = DEFAULT_INSTRUCTIONS,
    ):
        self.model_name = model or os.environ.get("OPENROUTER_MODEL") or MODEL
        self.base_url = base_url or os.environ.get("OPENROUTER_BASE_URL") or DEFAULT_BASE_URL
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
        if not self.api_key:
            raise RuntimeError("OPENROUTER_API_KEY must be set in the environment or .env file")

        self._agent: Agent[ThesauriToolsDependencies, str] = self._build_agent(instructions)

    def _build_agent(self, instructions: str) -> Agent[ThesauriToolsDependencies, str]:
        provider = OpenAIProvider(base_url=self.base_url, api_key=self.api_key)
        model = OpenAIChatModel(self.model_name, provider=provider)
        return build_thesauri_agent(
            model=model,
            deps_type=ThesauriToolsDependencies,
            instructions=instructions,
        )

    async def run(self, prompt: str, deps: LlmDeps) -> str:
        tool_deps = ThesauriToolsDependencies(api=deps.api, mapper=deps.mapper)
        result = await self._agent.run(prompt, deps=tool_deps)
        return result.output
