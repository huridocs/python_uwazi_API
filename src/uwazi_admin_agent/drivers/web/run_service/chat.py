"""Web driver module split out of the original ``run_service`` (behavior preserved)."""

from __future__ import annotations

from uwazi_admin_agent.domain.prompt_understanding import PromptUnderstanding
from uwazi_admin_agent.drivers.runtime import build_runtime
from uwazi_admin_agent.use_cases.clarify_prompt_use_case import ClarifyPromptUseCase
from uwazi_admin_agent.use_cases.compact_chat_use_case import CompactChatUseCase
from uwazi_agent.adapters.llm.ollama_adapter import OllamaAdapter


async def clarify_prompt(
    url: str,
    prompt: str,
    user: str,
    password: str,
    allowance_note: str = "",
) -> PromptUnderstanding:
    """Run the prompt-validation step: restate the prompt + surface ambiguities.

    A lightweight, tool-free LLM call that runs BEFORE generation so the operator
    can confirm/rephrase. ``allowance_note`` carries the operator-approved AI caps
    (the wizard's caps step, which runs before validation) so the clarifying
    questions match what the task can afford; empty when unset. Raises on LLM
    failure; the caller shows a skip affordance.
    """
    runtime = build_runtime(user=user, password=password, url=url)
    use_case = ClarifyPromptUseCase(llm=runtime.llm, deps=runtime.deps)
    return await use_case.execute(prompt, allowance_note=allowance_note)


async def compact_chat(conversation: str) -> str:
    """Rewrite a chat conversation as ONE self-contained final prompt.

    The Compact button's service: ONE tool-free LLM request that collapses the
    whole conversation (the original prompt, the resolved clarifications, every
    operator request and every clarification turn) into a single concise task
    description holding the conversation's final logic.

    It needs NO instance access — the rewrite is pure text — so it does NOT
    build the runtime (and does NOT log into the instance). Raises on LLM
    failure; the caller keeps the chat as it is.
    """
    use_case = CompactChatUseCase(llm=OllamaAdapter())
    return await use_case.execute(conversation)
