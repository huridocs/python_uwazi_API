"""The prompt-improvement chat's engine: one turn over a conversation.

Mirrors :class:`ClarifyPromptUseCase` (a tool-free pydantic-ai agent with an
``output_type``) but keeps the CONVERSATION: the assistant sees the whole chat
so far, not just the latest request. The agent is tool-free, so a turn is a
single LLM request — the same cheap shape as the wizard's validation step.

The system prompt is the clarification agent's contract plus the chat's own
rules (:data:`IMPROVE_PROMPT_SYSTEM_PROMPT_TEMPLATE`), with the instance's REAL
schema substituted in — that is what lets the assistant resolve property
names instead of asking "which property do you mean?".

Not unit-tested (it needs an LLM); its pure parts — the message rendering and
the final-prompt assembly — live in
:mod:`uwazi_admin_agent.domain.prompt_chat` and are unit-tested there.
"""

from __future__ import annotations

from pydantic_ai import Agent, UsageLimits
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models import Model

from uwazi_admin_agent.configuration import MAX_LLM_CALLS
from uwazi_admin_agent.domain.prompt_chat import ChatMessage, ChatTurn, render_chat_message
from uwazi_admin_agent.use_cases.admin_agent_deps import AdminAgentDeps
from uwazi_admin_agent.use_cases.clarify_schema_context import fetch_schema_context
from uwazi_admin_agent.use_cases.improve_prompt_system_prompt import IMPROVE_PROMPT_SYSTEM_PROMPT_TEMPLATE
from uwazi_agent.ports.llm_port import LlmPort


def build_prior_messages(history: list[ChatMessage]) -> list[ModelRequest]:
    """Render the chat so far as the pydantic-ai message history (pure).

    One ``ModelRequest`` per prior message, carrying that message's rendered
    text — the operator's request, or the assistant's summary + its questions +
    the revised prompt it proposed. The assistant therefore knows what it has
    ALREADY proposed in this conversation.
    """
    return [ModelRequest(parts=[UserPromptPart(content=render_chat_message(message))]) for message in history]


class ImprovePromptUseCase:
    """Improve a prompt through a multi-turn conversation (the chat's engine)."""

    def __init__(self, llm: LlmPort, deps: AdminAgentDeps | None = None) -> None:
        self._llm: LlmPort = llm
        # Wired during construction (build_runtime) so the system prompt embeds
        # the REAL instance schema — without it the assistant asks clarifying
        # questions ("which property?") the template definitions already answer.
        self._deps: AdminAgentDeps | None = deps
        # The clarification agent is tool-free, so one turn is one request; the
        # cap is a backstop against a runaway loop. The operator's own cap
        # (the cost dialog's request backstop) tightens it, never loosens it.
        self._max_requests: int = MAX_LLM_CALLS

    @staticmethod
    def _build_agent(model: Model, system_prompt: str) -> Agent[None, ChatTurn]:
        """Construct the pydantic-ai agent (no deps/llm instance needed)."""
        return Agent(
            model,
            system_prompt=system_prompt,
            output_type=ChatTurn,
        )

    async def execute(
        self,
        history: list[ChatMessage],
        request: str,
        max_requests: int | None = None,
    ) -> ChatTurn:
        """Run one turn over ``history`` + ``request``; return the assistant's answer.

        ``max_requests`` is the operator's approved request cap (the cost
        dialog's backstop). It TIGHTENS the built-in cap: the turn is a single
        tool-free request, so a cap of 1 is the normal case, and the agent
        aborts cleanly if it ever wanted more.
        """
        request_limit = self._max_requests if max_requests is None else min(max_requests, self._max_requests)
        system_prompt = IMPROVE_PROMPT_SYSTEM_PROMPT_TEMPLATE.replace(
            "{instance_schema}", await fetch_schema_context(self._deps)
        )
        agent = self._build_agent(self._llm.get_model(), system_prompt)
        run = await agent.run(
            request,
            usage_limits=UsageLimits(request_limit=max(1, request_limit)),
            message_history=build_prior_messages(history),
        )
        output = run.output
        if not isinstance(output, ChatTurn):
            raise RuntimeError(f"Agent returned an unsupported output type: {type(output).__name__}")
        return output


__all__ = ["ImprovePromptUseCase", "build_prior_messages"]
