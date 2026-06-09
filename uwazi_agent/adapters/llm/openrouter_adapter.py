import os

from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from uwazi_agent.configuration import MODEL
from uwazi_agent.ports.llm_port import LlmPort


DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterAdapter(LlmPort):
    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
    ):
        self.model_name = model or os.environ.get("OPENROUTER_MODEL") or MODEL
        self.base_url = base_url or os.environ.get("OPENROUTER_BASE_URL") or DEFAULT_BASE_URL
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
        if not self.api_key:
            raise RuntimeError("OPENROUTER_API_KEY must be set in the environment or .env file")

    def get_model(self) -> Model:
        provider = OpenAIProvider(base_url=self.base_url, api_key=self.api_key)
        return OpenAIChatModel(self.model_name, provider=provider)
