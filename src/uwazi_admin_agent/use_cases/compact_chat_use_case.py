"""Rewrite a chat conversation as ONE self-contained final prompt.

The prompt-improvement chat grows with every operator request and every
clarification turn, and the generation LLM receives the WHOLE conversation. The
Compact button uses this use case to collapse that history into a single
concise task description that holds the conversation's final logic.

A thin pydantic-ai ``Agent`` use case mirroring :class:`ClarifyPromptUseCase`
but with plain-text output: it is tool-free (the rewrite needs no instance
access), it runs on ONE request, and it returns the compacted prompt.

Not unit-tested (needs an LLM); the pure side — what a compacted history looks
like — is :func:`uwazi_admin_agent.domain.prompt_chat.compacted_history`.
"""

from __future__ import annotations

from pydantic_ai import Agent, UsageLimits
from pydantic_ai.models import Model

from uwazi_admin_agent.configuration import LLM_MAX_OUTPUT_TOKENS
from uwazi_admin_agent.use_cases.compact_chat_system_prompt import COMPACT_CHAT_SYSTEM_PROMPT
from uwazi_agent.ports.llm_port import LlmPort


class CompactChatUseCase:
    """Build the compaction agent, run it once, return the compacted prompt."""

    def __init__(self, llm: LlmPort) -> None:
        self._llm: LlmPort = llm

    @staticmethod
    def _build_agent(model: Model) -> Agent[None, str]:
        """Construct the pydantic-ai agent (no deps/llm instance needed)."""
        return Agent(
            model,
            system_prompt=COMPACT_CHAT_SYSTEM_PROMPT,
            output_type=str,
            model_settings={"max_tokens": LLM_MAX_OUTPUT_TOKENS},
        )

    async def execute(self, conversation: str) -> str:
        """Run the agent once over ``conversation`` and return the compacted prompt.

        Raises :class:`RuntimeError` when the model returns nothing usable: the
        caller keeps the chat as it is rather than replacing it with an empty
        prompt.
        """
        agent = self._build_agent(self._llm.get_model())
        run = await agent.run(
            f"Compact this prompt-improvement conversation:\n\n{conversation}",
            usage_limits=UsageLimits(request_limit=1),
        )
        output = str(getattr(run, "output", "") or "").strip()
        if not output:
            raise RuntimeError("The compaction returned an empty prompt.")
        return output


__all__ = ["CompactChatUseCase"]
