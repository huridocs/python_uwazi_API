import os

from pydantic_ai import Agent
from pydantic_ai.models.ollama import OllamaModel
from pydantic_ai.providers.ollama import OllamaProvider

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


class OllamaAdapter(LlmPort):
    def __init__(
        self,
        model: str | None = None,
        ollama_base_url: str | None = None,
        ollama_api_key: str | None = None,
        instructions: str = DEFAULT_INSTRUCTIONS,
    ):
        self.model_name = model or MODEL
        self.ollama_base_url = ollama_base_url or os.environ.get("OLLAMA_BASE_URL") or os.environ.get("OLLAMA_URL")
        self.ollama_api_key = ollama_api_key or os.environ.get("OLLAMA_API_KEY")
        if not self.ollama_base_url:
            raise RuntimeError("OLLAMA_BASE_URL (or OLLAMA_URL) must be set in the environment or .env file")
        if not self.ollama_api_key:
            raise RuntimeError("OLLAMA_API_KEY must be set in the environment or .env file")

        self._agent: Agent[ThesauriToolsDependencies, str] = self._build_agent(instructions)

    def _build_agent(self, instructions: str) -> Agent[ThesauriToolsDependencies, str]:
        provider = OllamaProvider(base_url=self.ollama_base_url, api_key=self.ollama_api_key)
        model = OllamaModel(self.model_name, provider=provider)
        return build_thesauri_agent(
            model=model,
            deps_type=ThesauriToolsDependencies,
            instructions=instructions,
        )

    async def run(self, prompt: str, deps: LlmDeps) -> str:
        tool_deps = ThesauriToolsDependencies(api=deps.api, mapper=deps.mapper)
        result = await self._agent.run(prompt, deps=tool_deps)
        return result.output
