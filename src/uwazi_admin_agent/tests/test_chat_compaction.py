"""Isolated unit tests for the chat COMPACTION's pure seams (per AGENTS.md).

The compaction itself is an LLM call (not unit-testable), so what is tested
here is everything around it: what text the compactor receives, what the
compacted chat's history looks like, what the operator sees, and what the
generation LLM receives afterwards.

No mocks, no network, no env credentials.
"""

from __future__ import annotations

from uwazi_admin_agent.domain.prompt_chat import (
    ChatMessage,
    ChatPromptStatus,
    ChatRole,
    apply_compaction,
    build_compaction_input,
    build_final_prompt_from_chat,
)
from uwazi_admin_agent.drivers.web.prompt_chat_dialog import _compacted_bubbles

ORIGINAL = "Set the document reference on every case file."


def _original() -> ChatMessage:
    return ChatMessage(role=ChatRole.SYSTEM, text=ORIGINAL)


def _operator(text: str, status: ChatPromptStatus = ChatPromptStatus.PENDING) -> ChatMessage:
    return ChatMessage(role=ChatRole.OPERATOR, text=text, status=status)


def test_the_compactor_receives_the_original_prompt_and_the_conversation() -> None:
    """The compactor needs both halves: what the run was, and what changed."""
    payload = build_compaction_input([_original(), _operator("also set the case reference")])

    assert ORIGINAL in payload
    assert "also set the case reference" in payload
    assert payload.index(ORIGINAL) < payload.index("also set the case reference")


def test_the_compaction_input_carries_no_empty_conversation_section() -> None:
    """A chat with no request yet has nothing to compact beyond the prompt."""
    payload = build_compaction_input([_original()])

    assert ORIGINAL in payload
    assert "CONVERSATION:" not in payload


def test_a_compacted_chat_keeps_the_run_context_and_drops_the_conversation() -> None:
    """The compaction replaces the CONVERSATION; the run's own context survives."""
    history = [
        _original(),
        _operator("also set the case reference"),
        ChatMessage(role=ChatRole.ASSISTANT, text="Understood.", status=ChatPromptStatus.APPLIED),
    ]

    compacted = apply_compaction(history, "Set the document and case reference on every case file.")

    assert [message.role for message in compacted] == [ChatRole.SYSTEM, ChatRole.OPERATOR]
    assert compacted[0].text == ORIGINAL
    assert compacted[1].text == "Set the document and case reference on every case file."
    assert "also set the case reference" not in build_final_prompt_from_chat(compacted)


def test_a_compacted_prompt_is_pending_when_a_request_waited_for_the_run() -> None:
    """The compaction does NOT run anything: an unrun request stays unrun."""
    history = [
        _original(),
        _operator("also set the case reference", ChatPromptStatus.PENDING),
    ]

    compacted = apply_compaction(history, "Set both references.")

    assert compacted[-1].status is ChatPromptStatus.PENDING


def test_a_compacted_prompt_stays_applied_when_every_request_was_already_run() -> None:
    """An already-run chat compacts into an applied prompt: no logic changed."""
    history = [
        _original(),
        _operator("also set the case reference", ChatPromptStatus.APPLIED),
    ]

    compacted = apply_compaction(history, "Set both references.")

    assert compacted[-1].status is ChatPromptStatus.APPLIED


def test_the_compacted_chat_shows_the_base_prompt_and_one_compacted_bubble() -> None:
    """The operator sees the run's prompt plus the ONE rewritten prompt."""
    history = [
        _original(),
        _operator("also set the case reference"),
        ChatMessage(role=ChatRole.ASSISTANT, text="Understood.", status=ChatPromptStatus.APPLIED),
    ]

    compacted = apply_compaction(history, "Set both references.", "12:00")
    bubbles = _compacted_bubbles(compacted)

    assert [label for label, *_rest in bubbles] == ["Base prompt", "Compacted prompt"]
    assert bubbles[1][1] == "Set both references."
    assert bubbles[1][2] == "12:00"
    assert bubbles[1][3] is ChatPromptStatus.PENDING


def test_the_generation_llm_receives_the_compacted_prompt_and_the_original() -> None:
    """After a compaction the generation LLM sees the final logic, not the chat."""
    history = [
        _original(),
        _operator("also set the case reference"),
        _operator("and delete the second entity afterwards"),
    ]

    compacted = apply_compaction(history, "Set both references; delete the second entity.")
    final = build_final_prompt_from_chat(compacted)

    assert final.startswith(ORIGINAL)
    assert "Set both references; delete the second entity." in final
    assert "also set the case reference" not in final
    assert "delete the second entity afterwards" not in final
