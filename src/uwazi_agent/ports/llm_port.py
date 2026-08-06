from abc import ABC, abstractmethod

from pydantic_ai.models import Model


class LlmPort(ABC):
    """Provider-agnostic LLM port.

    Implementations (Ollama, OpenRouter, ...) create a pydantic-ai Model.
    The use case is responsible for building the Agent and running it.
    """

    @abstractmethod
    def get_model(self) -> Model:
        """Return the pydantic-ai Model to use in the Agent."""
        pass
