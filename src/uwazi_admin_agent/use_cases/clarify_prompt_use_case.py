"""Turn a prompt into a :class:`PromptUnderstanding` (the prompt-validation step).

A thin pydantic-ai ``Agent`` use case mirroring :class:`GenerateScriptUseCase` but
tool-free and cheap: it restates the prompt and surfaces clarifying questions so
the operator can confirm/rephrase BEFORE the expensive generation call runs. The
structured :class:`PromptUnderstanding` is the result type.

Not unit-tested (needs an LLM); validated by import + agent construction with a
string model name (construction is lazy — no provider contact until ``run``).
"""

from __future__ import annotations

from pydantic_ai import Agent
from pydantic_ai.models import Model

from uwazi_admin_agent.domain.prompt_understanding import PromptUnderstanding
from uwazi_admin_agent.use_cases.clarify_system_prompt import CLARIFY_SYSTEM_PROMPT
from uwazi_agent.ports.llm_port import LlmPort


class ClarifyPromptUseCase:
    """Build the clarification agent, run it once, return a :class:`PromptUnderstanding`."""

    def __init__(self, llm: LlmPort) -> None:
        self._llm: LlmPort = llm

    @staticmethod
    def _build_agent(model: Model) -> Agent[None, PromptUnderstanding]:
        """Construct the pydantic-ai agent (no deps/llm instance needed)."""
        return Agent(
            model,
            system_prompt=CLARIFY_SYSTEM_PROMPT,
            output_type=PromptUnderstanding,
        )

    async def execute(self, prompt: str) -> PromptUnderstanding:
        """Run the agent once on ``prompt`` and return its understanding."""
        agent = self._build_agent(self._llm.get_model())
        run = await agent.run(prompt)
        output = run.output
        if not isinstance(output, PromptUnderstanding):
            raise RuntimeError(f"Agent returned an unsupported output type: {type(output).__name__}")
        return output
