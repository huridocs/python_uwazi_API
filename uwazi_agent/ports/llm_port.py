from abc import ABC, abstractmethod

from pydantic import BaseModel

from uwazi_agent.ports.mapper_port import ThesauriMapperPort
from uwazi_agent.ports.uwazi_api_port import ThesauriApiPort


class LlmDeps(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    api: ThesauriApiPort
    mapper: ThesauriMapperPort | None = None


class LlmPort(ABC):
    """Provider-agnostic LLM port.

    Implementations (Ollama, OpenRouter, ...) build a pydantic-ai Agent under
    the hood, expose the thesaurus tools to it, and forward the call to the
    underlying model. The use case is unaware of which provider is plugged in.
    """

    @abstractmethod
    async def run(
        self,
        prompt: str,
        deps: LlmDeps,
    ) -> str:
        """Run the LLM on the given prompt and return its final text output.

        The implementation is responsible for forwarding ``deps`` to whichever
        tools the LLM is allowed to call.
        """
        pass
