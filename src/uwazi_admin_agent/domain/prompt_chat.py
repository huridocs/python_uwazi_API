"""The prompt-improvement chat: its messages, its answers, and their rendering.

The chat is the operator-facing way to IMPROVE a run's prompt after the fact.
It opens with the run's ORIGINAL prompt and the validation step's resolved
clarifications (both read-only context), then the operator types free-form
requests. A request is recorded as one OPERATOR message: its text is the
prompt, and once the clarification step's answers are resolved they are folded
into that SAME text in the wizard's ``Clarifications (resolved during
validation):`` format. The optional assistant-turn model (:class:`ChatTurn`)
remains available for an assistant that answers with a summary, questions and
a revised prompt.

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

import re
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from uwazi_admin_agent.domain.clarifying_question import ClarifyingQuestion
from uwazi_admin_agent.domain.prompt_validation import QuestionAnswer, build_final_prompt


class ChatRole(str, Enum):
    """Who said a chat message."""

    OPERATOR = "operator"  # what the user typed
    ASSISTANT = "assistant"  # the LLM's answer
    SYSTEM = "system"  # the original prompt + the validation clarifications


class ChatPromptStatus(str, Enum):
    """Whether an operator prompt has been folded into a Run.

    The chat shows one bubble per prompt, so the operator can see WHICH request
    is still waiting to be regenerated. Only operator messages carry a
    meaningful status; a SYSTEM/ASSISTANT message's status is ignored.
    """

    PENDING = "pending"  # written by the operator, not run yet
    APPLIED = "applied"  # part of a completed Run's prompt


class ChatMessage(BaseModel):
    """One message in the prompt-improvement chat."""

    model_config = ConfigDict(frozen=True)

    role: ChatRole
    text: str
    stamp: str = ""  # ISO timestamp, for display
    status: ChatPromptStatus = ChatPromptStatus.PENDING
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

# A message heading inside the conversation block, as ``build_chat_history``
# writes it. Parsing it back is what lets a re-opened chat show ONE BUBBLE PER
# PROMPT instead of one bubble holding the whole conversation.
_MESSAGE_HEADING_RE: re.Pattern[str] = re.compile(r"^## (Operator|Assistant)\s*$")
_REVISED_PROMPT_HEADING: str = "### Revised prompt"


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


def render_question_answers(answers: list[QuestionAnswer]) -> str:
    """Render the operator's ticked answers as the next chat request (pure).

    The text is what the operator "says" after answering a turn's clarifying
    questions, so it is recorded as an OPERATOR message. Questions with neither
    a ticked option nor a custom answer are omitted: the operator skipped them,
    and the assistant can ask again.
    """
    lines: list[str] = ["Answers to your clarifying questions:"]
    for answer in answers:
        custom = (answer.custom or "").strip()
        if not answer.selected and not custom:
            continue
        lines.append(f"- {answer.question}")
        for option in answer.selected:
            lines.append(f"  - {option}")
        if custom:
            lines.append(f"  - {custom}")
    return "\n".join(lines) if len(lines) > 1 else ""


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


def _split_revised_prompt(body: str) -> tuple[str, str | None]:
    """Split an assistant block into its prose/questions and its revised prompt (pure)."""
    lines = body.splitlines()
    for index, line in enumerate(lines):
        if line.strip() == _REVISED_PROMPT_HEADING:
            head = "\n".join(lines[:index]).strip()
            revised = "\n".join(lines[index + 1 :]).strip()
            return head, revised or None
    return body.strip(), None


def parse_conversation(conversation: str) -> list[ChatMessage]:
    """Parse a rendered ``Conversation:`` block back into its messages (pure).

    ``build_final_prompt_from_chat`` writes the conversation with
    :func:`render_chat_message`; this is the inverse for the parts a re-opened
    chat needs. Every parsed message is marked
    :attr:`ChatPromptStatus.APPLIED`: the conversation is part of the prompt
    that produced the run's CURRENT script, so those requests were run.
    """
    messages: list[ChatMessage] = []
    role: ChatRole | None = None
    body: list[str] = []

    def _flush() -> None:
        if role is None:
            return
        text = "\n".join(body).strip()
        if not text:
            return
        if role is ChatRole.OPERATOR:
            messages.append(ChatMessage(role=ChatRole.OPERATOR, text=text, status=ChatPromptStatus.APPLIED))
            return
        summary, revised = _split_revised_prompt(text)
        messages.append(
            ChatMessage(
                role=ChatRole.ASSISTANT,
                text=summary,
                status=ChatPromptStatus.APPLIED,
                revised_prompt=revised,
            )
        )

    for line in conversation.splitlines():
        match = _MESSAGE_HEADING_RE.match(line)
        if match is not None:
            _flush()
            role = ChatRole(match.group(1).lower())
            body = []
            continue
        if role is not None:
            body.append(line)
    _flush()
    return messages


def reconstruct_chat(original: str, validated: str) -> tuple[str, list[ChatMessage]]:
    """Rebuild a RE-OPENED chat from the run's persisted prompt fields (pure).

    Returns ``(baseline, messages)``:

    - ``baseline`` is the one bubble that describes the run itself: the
      original prompt plus the validation step's clarifications, WITHOUT any
      conversation (which gets a bubble per prompt instead);
    - ``messages`` is the history the generation LLM is replayed, and it mirrors
      :func:`build_final_prompt_from_chat`'s two inputs: the original prompt as
      the first ``SYSTEM`` message, the clarifications as a second one when
      there are any, then one ``OPERATOR`` message per prior request.

    A request found in the persisted conversation was part of the Run that
    produced the current script, so it is marked ``APPLIED``. Requests written
    in THIS dialog session stay ``PENDING`` until the Run button succeeds.
    """
    original = (original or "").strip()
    validated = (validated or "").strip()
    baseline, conversation = _split_validation(validated) if validated else ("", "")
    prior = parse_conversation(conversation)

    messages: list[ChatMessage] = []
    if original:
        messages.append(ChatMessage(role=ChatRole.SYSTEM, text=original))
    if baseline and baseline != original:
        clarifications = baseline
        if original and clarifications.startswith(original):
            clarifications = clarifications[len(original) :].strip()
        if clarifications:
            messages.append(ChatMessage(role=ChatRole.SYSTEM, text=clarifications))
    messages.extend(prior)
    return (baseline or original), messages


def mark_prompts_applied(messages: list[ChatMessage]) -> list[ChatMessage]:
    """Return ``messages`` with every operator prompt marked applied (pure).

    Called after a successful Run: the requests the operator just wrote are now
    part of the script's prompt, so their bubbles must stop saying "not run
    yet". Idempotent: already-applied prompts and non-operator messages are
    returned unchanged.
    """
    return [
        message.model_copy(update={"status": ChatPromptStatus.APPLIED})
        if message.role is ChatRole.OPERATOR and message.status is not ChatPromptStatus.APPLIED
        else message
        for message in messages
    ]


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
    "ChatPromptStatus",
    "ChatRole",
    "ChatTurn",
    "build_chat_history",
    "build_final_prompt_from_chat",
    "mark_prompts_applied",
    "parse_conversation",
    "reconstruct_chat",
    "render_chat_message",
    "render_question_answers",
]
