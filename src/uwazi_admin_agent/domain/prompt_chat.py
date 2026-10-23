"""The prompt-improvement chat: its messages, its answers, and their rendering.

The chat is the operator-facing way to IMPROVE a run's prompt after the fact.
It opens with the run's ORIGINAL prompt and the validation step's resolved
clarifications (both read-only context), then the operator types free-form
requests. Every turn the assistant answers with a :class:`ChatTurn`: what it
understood, the clarifying questions it still has, and the COMPLETE revised
prompt it proposes.

The rendering helpers are PURE (no I/O) so the shapes that matter — what the
assistant sees when the chat is replayed, and what the generation LLM receives
when the operator hits Run — are unit-testable without an LLM.

``build_final_prompt_from_chat`` is the seam between the chat and the
generation agent: it folds the original prompt, the resolved clarifications and
the WHOLE conversation into the single prompt the generation LLM receives. The
clarification block is rendered with :func:`uwazi_admin_agent.domain.
prompt_validation.build_final_prompt` — the SAME formatter the wizard's
validation step uses — so both prompts are formatted identically.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from uwazi_admin_agent.domain.clarifying_question import ClarifyingQuestion
from uwazi_admin_agent.domain.prompt_validation import QuestionAnswer, build_final_prompt


class ChatRole(str, Enum):
    """Who said a chat message."""

    OPERATOR = "operator"  # what the user typed
    ASSISTANT = "assistant"  # the LLM's answer
    SYSTEM = "system"  # the original prompt + the validation clarifications


class ChatMessage(BaseModel):
    """One message in the prompt-improvement chat."""

    model_config = ConfigDict(frozen=True)

    role: ChatRole
    text: str
    stamp: str = ""  # ISO timestamp, for display
    questions: list[ClarifyingQuestion] = Field(default_factory=list)
    revised_prompt: str | None = None


class ChatTurn(BaseModel):
    """One assistant answer: the questions it raised + the prompt it proposes."""

    model_config = ConfigDict(frozen=True)

    summary: str = Field(description="What the assistant understood the request to mean.")
    questions: list[ClarifyingQuestion] = Field(default_factory=list)
    revised_prompt: str = Field(description="The full revised prompt, ready to be sent to the generation LLM.")


_HEADINGS: dict[ChatRole, str] = {
    ChatRole.OPERATOR: "## Operator",
    ChatRole.ASSISTANT: "## Assistant",
    ChatRole.SYSTEM: "## System",
}

# The heading the conversation is rendered under (in the final prompt AND in a
# re-opened chat's validation message, which carries the chat-derived prompt).
_CONVERSATION_HEADING: str = "Conversation:"


def render_chat_message(message: ChatMessage) -> str:
    """Render ONE chat message as the plain-text block the LLM sees (pure)."""
    lines: list[str] = [_HEADINGS[message.role]]
    text = message.text.strip()
    if text:
        lines.append(text)
    if message.questions:
        lines.append("")
        for question in message.questions:
            lines.append(f"- {question.question}")
            for option in question.options:
                lines.append(f"  - {option}")
    if message.revised_prompt:
        lines.append("")
        lines.append("### Revised prompt")
        lines.append(message.revised_prompt.strip())
    return "\n".join(lines)


def build_chat_history(messages: list[ChatMessage]) -> str:
    """Render the chat so far as one plain-text block (pure)."""
    return "\n\n".join(render_chat_message(message) for message in messages)


def _split_validation(text: str) -> tuple[str, str]:
    """Split a validation message into its clarifications and its conversation.

    The validation message is EITHER the wizard's validated prompt (the
    original plus a ``Clarifications (resolved during validation):`` block) OR
    — after a chat Run — the chat-derived prompt (the original plus a
    ``Conversation:`` block). Splitting on the conversation heading keeps the
    two apart, so a re-opened chat does not fold the PREVIOUS conversation into
    the clarifications and repeat it.
    """
    if _CONVERSATION_HEADING in text:
        head, _, tail = text.partition(_CONVERSATION_HEADING)
        return head.strip(), tail.strip()
    return text.strip(), ""


def build_final_prompt_from_chat(messages: list[ChatMessage]) -> str:
    """The prompt the generation LLM receives: the original + the whole chat.

    Pure: no I/O. The result carries

    1. the original prompt (the FIRST ``SYSTEM`` message),
    2. the resolved clarifications from the validation step — verbatim when the
       validation message carries the wizard's already-formatted block, and
       rendered with :func:`build_final_prompt` when it carries structured
       answers, so the two paths are formatted identically, and
    3. the conversation: every operator request and every revised prompt the
       assistant proposed, under a ``Conversation:`` heading. A validation
       message that already carries a conversation (a re-opened chat after a
       Run) contributes it as the PRIOR conversation, so the history is
       continued rather than repeated.
    """
    system_messages = [m for m in messages if m.role is ChatRole.SYSTEM]
    original = system_messages[0].text.strip() if system_messages else ""

    # The clarifications: either the wizard's already-formatted block (the
    # message's text) or structured answers (the message's questions), rendered
    # by the SAME formatter the wizard uses so both prompts read identically.
    clarifications = ""
    prior_conversation = ""
    for message in system_messages[1:]:
        text = message.text.strip()
        if text:
            text, conversation = _split_validation(text)
            if original and text.startswith(original):
                text = text[len(original) :].strip()
            if text:
                clarifications = text
            if conversation:
                prior_conversation = conversation
            continue
        answers = [QuestionAnswer(question=q.question, selected=list(q.options)) for q in message.questions]
        if not answers:
            continue
        rendered = build_final_prompt(original, answers, notes="").strip()
        block = rendered[len(original) :].strip() if rendered.startswith(original) else rendered
        if block:
            clarifications = f"{clarifications}\n\n{block}" if clarifications else block

    parts = [original] if original else []
    if clarifications:
        parts.append(clarifications)

    conversation = build_chat_history([m for m in messages if m.role in (ChatRole.OPERATOR, ChatRole.ASSISTANT)])
    if prior_conversation:
        conversation = f"{prior_conversation}\n\n{conversation}" if conversation else prior_conversation
    if conversation:
        parts.append(f"{_CONVERSATION_HEADING}\n{conversation}")

    return "\n\n".join(parts)


__all__ = [
    "ChatMessage",
    "ChatRole",
    "ChatTurn",
    "build_chat_history",
    "build_final_prompt_from_chat",
    "render_chat_message",
]
