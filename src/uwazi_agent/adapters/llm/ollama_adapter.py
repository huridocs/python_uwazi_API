import os

from pydantic_ai.models import Model
from pydantic_ai.models.ollama import OllamaModel
from pydantic_ai.providers.ollama import OllamaProvider

from uwazi_agent.configuration import MODEL, OLLAMA_BASE_URL
from uwazi_agent.ports.llm_port import LlmPort


class OllamaAdapter(LlmPort):
    def __init__(
        self,
        model: str | None = None,
        ollama_base_url: str | None = None,
        ollama_api_key: str | None = None,
    ):
        self.model_name = model or MODEL
        self.ollama_base_url = OLLAMA_BASE_URL
        self.ollama_api_key = ollama_api_key or os.environ.get("OLLAMA_API_KEY")
        if not self.ollama_api_key:
            raise RuntimeError("OLLAMA_API_KEY must be set in the environment or .env file")

    def get_model(self) -> Model:
        provider = OllamaProvider(base_url=self.ollama_base_url, api_key=self.ollama_api_key)
        return OllamaModel(self.model_name, provider=provider)
