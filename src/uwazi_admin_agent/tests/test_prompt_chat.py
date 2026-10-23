"""Isolated unit tests for the prompt-improvement chat's pure rendering (per AGENTS.md).

Pure functions over literal inputs: no mocks, no network, no env credentials.
The chat's LLM-backed parts (the turn itself) are not unit-tested — the pure
seams that decide what the assistant sees and what the generation LLM receives
are.
"""

from __future__ import annotations

from uwazi_admin_agent.domain.clarifying_question import ClarifyingQuestion
from uwazi_admin_agent.domain.prompt_chat import (
    ChatMessage,
    ChatRole,
    ChatTurn,
    build_chat_history,
    build_final_prompt_from_chat,
)

ORIGINAL = "Set the document reference on every case file."


def _operator(text: str) -> ChatMessage:
    return ChatMessage(role=ChatRole.OPERATOR, text=text)


def _assistant(
    summary: str,
    revised: str,
    questions: list[ClarifyingQuestion] | None = None,
) -> ChatMessage:
    return ChatMessage(
        role=ChatRole.ASSISTANT,
        text=summary,
        questions=questions or [],
        revised_prompt=revised,
    )


def _original() -> ChatMessage:
    return ChatMessage(role=ChatRole.SYSTEM, text=ORIGINAL)


def test_build_chat_history_renders_operator_and_assistant_messages() -> None:
    rendered = build_chat_history(
        [
            _operator("also set the document reference"),
            _assistant(
                "You want the document reference set as well.",
                "Set the document reference and the case reference on every case file.",
                [ClarifyingQuestion(question="Which property carries it?", options=["document_ref", "case_ref"])],
            ),
        ]
    )

    assert "## Operator" in rendered
    assert "also set the document reference" in rendered
    assert "## Assistant" in rendered
    assert "You want the document reference set as well." in rendered
    assert "Which property carries it?" in rendered
    assert "  - document_ref" in rendered
    assert "### Revised prompt" in rendered
    assert "Set the document reference and the case reference" in rendered


def test_build_final_prompt_from_chat_keeps_the_original_prompt_first() -> None:
    final = build_final_prompt_from_chat([_original()])
    assert final == ORIGINAL


def test_build_final_prompt_from_chat_includes_the_validation_clarifications() -> None:
    validation = ChatMessage(
        role=ChatRole.SYSTEM,
        text="",
        questions=[ClarifyingQuestion(question="Which property?", options=["document_ref"])],
    )
    final = build_final_prompt_from_chat([_original(), validation])

    assert final.startswith(ORIGINAL)
    assert "Clarifications (resolved during validation):" in final
    assert "- Which property?" in final
    assert "  - document_ref" in final


def test_build_final_prompt_from_chat_includes_every_turn() -> None:
    final = build_final_prompt_from_chat(
        [
            _original(),
            _operator("also set the document reference"),
            _assistant("Understood.", "Set the document reference on every case file."),
        ]
    )

    assert final.startswith(ORIGINAL)
    assert "also set the document reference" in final
    assert "Set the document reference on every case file." in final
    assert "Conversation:" in final


def test_build_final_prompt_from_chat_without_assistant_turns_returns_the_original() -> None:
    final = build_final_prompt_from_chat([_original(), _operator("just checking")])
    assert final.startswith(ORIGINAL)
    assert "just checking" in final


def test_build_final_prompt_from_chat_carries_the_wizard_formatted_validation_block() -> None:
    """A validation message that already carries the wizard's block is kept verbatim."""
    block = "\n\nClarifications (resolved during validation):\n- Which property?\n  - document_ref"
    validation = ChatMessage(role=ChatRole.SYSTEM, text=ORIGINAL + block)
    final = build_final_prompt_from_chat([_original(), validation])

    assert final.startswith(ORIGINAL)
    assert "Clarifications (resolved during validation):" in final
    # The original prompt is not repeated inside the clarification block.
    assert final.count(ORIGINAL) == 1


def test_a_reopened_chat_does_not_repeat_the_previous_conversation() -> None:
    """After a Run the manifest carries the chat-derived prompt as the validation.

    Re-opening the chat therefore shows that prompt as its validation message —
    and it must NOT be folded into the clarifications and repeated a second
    time in the next final prompt.
    """
    prior = (
        f"{ORIGINAL}\n\nConversation:\n## Operator\nalso set the document reference\n\n"
        "## Assistant\nUnderstood.\n\n### Revised prompt\nSet both."
    )
    final = build_final_prompt_from_chat(
        [
            _original(),
            ChatMessage(role=ChatRole.SYSTEM, text=prior),
            _operator("and the case reference"),
        ]
    )

    assert final.count("also set the document reference") == 1
    assert final.count("Conversation:") == 1
    assert "and the case reference" in final
    # The prior conversation is CONTINUED, not repeated.
    assert final.index("also set the document reference") < final.index("and the case reference")


def test_a_chat_turn_carries_the_questions_and_the_revised_prompt() -> None:
    turn = ChatTurn(
        summary="Understood.",
        questions=[ClarifyingQuestion(question="Which property?")],
        revised_prompt="Set the document reference.",
    )
    assert turn.summary == "Understood."
    assert turn.questions[0].question == "Which property?"
    assert turn.revised_prompt == "Set the document reference."


def test_a_chat_message_defaults_to_no_questions_and_no_revised_prompt() -> None:
    message = ChatMessage(role=ChatRole.OPERATOR, text="hello")
    assert message.questions == []
    assert message.revised_prompt is None
    assert message.stamp == ""
