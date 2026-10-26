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
    ChatPromptStatus,
    ChatRole,
    ChatTurn,
    build_chat_history,
    build_final_prompt_from_chat,
    mark_prompts_applied,
    parse_conversation,
    reconstruct_chat,
    render_chat_message,
    render_question_answers,
)
from uwazi_admin_agent.domain.prompt_validation import QuestionAnswer
from uwazi_admin_agent.drivers.web.prompt_chat_dialog import (
    _PROMPT_BUBBLE_CSS,
    PROMPT_BUBBLE_BACKGROUND,
    PROMPT_BUBBLE_CLASS,
    _initial_bubbles,
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
    assert message.status is ChatPromptStatus.PENDING


def test_render_chat_message_ignores_the_run_status() -> None:
    """Status is UI-only: the LLM prompt must not change when a prompt is run."""
    pending = ChatMessage(role=ChatRole.OPERATOR, text="also set the reference")
    applied = pending.model_copy(update={"status": ChatPromptStatus.APPLIED})
    assert render_chat_message(pending) == render_chat_message(applied)


def test_reconstruct_chat_gives_every_prior_prompt_its_own_applied_message() -> None:
    """The reported bug: a re-opened chat folded base prompt and requests into one.

    The persisted prompt carries the original, its clarifications and a
    ``Conversation:`` block. Reconstruction returns the base prompt WITHOUT the
    conversation, plus one applied message per prior request.
    """
    validated = (
        f"{ORIGINAL}\n\n"
        "Clarifications (resolved during validation):\n"
        "- Which property?\n"
        "  - document_ref\n\n"
        "Conversation:\n"
        "## Operator\n"
        "I change my mind, delete the second entity afterwards\n\n"
        "## Operator\n"
        "and keep the attachments"
    )

    baseline, messages = reconstruct_chat(ORIGINAL, validated)

    assert baseline.startswith(ORIGINAL)
    assert "Clarifications (resolved during validation):" in baseline
    assert "Conversation:" not in baseline
    assert "I change my mind" not in baseline

    operators = [message for message in messages if message.role is ChatRole.OPERATOR]
    assert [message.text for message in operators] == [
        "I change my mind, delete the second entity afterwards",
        "and keep the attachments",
    ]
    assert all(message.status is ChatPromptStatus.APPLIED for message in operators)

    system = [message for message in messages if message.role is ChatRole.SYSTEM]
    assert system[0].text == ORIGINAL
    assert "Clarifications (resolved during validation):" in system[1].text
    # The original prompt is kept ONCE, as the first system message.
    assert ORIGINAL not in system[1].text


def test_reconstruct_chat_of_a_plain_validated_prompt_has_no_prior_prompts() -> None:
    validated = f"{ORIGINAL}\n\nClarifications (resolved during validation):\n- Which property?\n  - document_ref"

    baseline, messages = reconstruct_chat(ORIGINAL, validated)

    assert baseline == validated
    assert [message.role for message in messages] == [ChatRole.SYSTEM, ChatRole.SYSTEM]
    assert not [message for message in messages if message.role is ChatRole.OPERATOR]


def test_a_reopened_chat_continues_every_prompt_exactly_once() -> None:
    """The rebuilt history must replay the prior requests without repeating them."""
    validated = (
        f"{ORIGINAL}\n\nConversation:\n"
        "## Operator\nI change my mind, delete the second entity afterwards\n\n"
        "## Operator\nand keep the attachments"
    )
    baseline, history = reconstruct_chat(ORIGINAL, validated)
    history.append(ChatMessage(role=ChatRole.OPERATOR, text="also set the reference", stamp="10:00"))

    final = build_final_prompt_from_chat(history)

    assert baseline == ORIGINAL
    assert final.count("I change my mind") == 1
    assert final.count("and keep the attachments") == 1
    assert final.count("also set the reference") == 1
    assert final.count("Conversation:") == 1
    assert final.index("I change my mind") < final.index("and keep the attachments")
    assert final.index("and keep the attachments") < final.index("also set the reference")


def test_a_run_then_reopen_round_trips_one_bubble_per_prompt() -> None:
    """The exact reported flow: run a change, close, reopen, see two bubbles.

    ``build_final_prompt_from_chat`` is what the Run persists; reconstruction
    must split that output back into the base prompt and the request(s).
    """
    clarifications = ChatMessage(
        role=ChatRole.SYSTEM,
        text="",
        questions=[ClarifyingQuestion(question="Which property?", options=["document_ref"])],
    )
    run_request = _operator("I change my mind, delete the second entity afterwards").model_copy(
        update={"status": ChatPromptStatus.APPLIED}
    )
    validated = build_final_prompt_from_chat([_original(), clarifications, run_request])

    baseline, messages = reconstruct_chat(ORIGINAL, validated)

    operators = [message for message in messages if message.role is ChatRole.OPERATOR]
    assert [message.text for message in operators] == [run_request.text]
    assert operators[0].status is ChatPromptStatus.APPLIED
    assert "Clarifications (resolved during validation):" in baseline
    assert run_request.text not in baseline


def test_reopened_chat_renders_a_bubble_for_every_prior_prompt() -> None:
    """The regression guard: rendering must not collapse the recovered prompts.

    Reconstructing the messages is not enough — each one has to become its own
    bubble. ``_initial_bubbles`` is the pure display plan the dialog renders.
    """
    validated = (
        f"{ORIGINAL}\n\nConversation:\n## Operator\nfirst change\n\n## Operator\nsecond change\n\n## Operator\nthird change"
    )
    baseline, history = reconstruct_chat(ORIGINAL, validated)

    bubbles = _initial_bubbles(baseline, history)

    assert [label for label, _text, _stamp, _status in bubbles] == [
        "Base prompt",
        "Prompt",
        "Prompt",
        "Prompt",
    ]
    assert [text for _label, text, _stamp, _status in bubbles] == [
        baseline,
        "first change",
        "second change",
        "third change",
    ]
    assert all(status is ChatPromptStatus.APPLIED for _label, _text, _stamp, status in bubbles)


def test_parse_conversation_round_trips_an_assistant_turn() -> None:
    rendered = build_chat_history(
        [
            _operator("also set the document reference"),
            _assistant(
                "You want the document reference set as well.",
                "Set the document reference and the case reference on every case file.",
                [ClarifyingQuestion(question="Which property carries it?", options=["document_ref"])],
            ),
        ]
    )

    parsed = parse_conversation(rendered)

    assert [message.role for message in parsed] == [ChatRole.OPERATOR, ChatRole.ASSISTANT]
    assert parsed[0].text == "also set the document reference"
    assert "You want the document reference set as well." in parsed[1].text
    assert "Which property carries it?" in parsed[1].text
    assert parsed[1].revised_prompt == "Set the document reference and the case reference on every case file."
    assert all(message.status is ChatPromptStatus.APPLIED for message in parsed)


def test_mark_prompts_applied_flips_only_operator_prompts() -> None:
    history = [
        _original(),
        _operator("not run yet"),
        _assistant("Understood.", "A prompt."),
    ]

    updated = mark_prompts_applied(history)

    assert updated[0].status is ChatPromptStatus.PENDING
    assert updated[1].status is ChatPromptStatus.APPLIED
    assert updated[2].status is ChatPromptStatus.PENDING
    # The input list is not mutated: history stays the source of truth.
    assert history[1].status is ChatPromptStatus.PENDING


def test_render_question_answers_lists_every_ticked_option_and_custom_answer() -> None:
    request = render_question_answers(
        [
            QuestionAnswer(
                question="Which property carries the reference?",
                selected=["document_ref", "case_ref"],
            ),
            QuestionAnswer(question="Should the old value be kept?", custom="No, overwrite it."),
        ]
    )

    assert request.startswith("Answers to your clarifying questions:")
    assert "- Which property carries the reference?" in request
    assert "  - document_ref" in request
    assert "  - case_ref" in request
    assert "- Should the old value be kept?" in request
    assert "  - No, overwrite it." in request


def test_prompt_bubble_css_paints_received_bubbles_with_the_requested_color() -> None:
    """The bubble background is the requested color, on received prompt bubbles.

    Quasar fills a received bubble with ``currentColor``, so the color has to be
    set on ``.q-message-text--received`` scoped to the prompt-bubble class (the
    inner content stays black).
    """
    assert PROMPT_BUBBLE_BACKGROUND == "rgb(198, 220, 236)"
    assert f".{PROMPT_BUBBLE_CLASS} .q-message-text--received" in _PROMPT_BUBBLE_CSS
    assert PROMPT_BUBBLE_BACKGROUND in _PROMPT_BUBBLE_CSS


def test_render_question_answers_omits_unanswered_questions() -> None:
    request = render_question_answers(
        [
            QuestionAnswer(
                question="Which property?",
                selected=["document_ref"],
            ),
            QuestionAnswer(question="Skipped question?", selected=[]),
            QuestionAnswer(question="Blank custom?", custom="   "),
        ]
    )

    assert "Which property?" in request
    assert "Skipped question?" not in request
    assert "Blank custom?" not in request


def test_render_question_answers_is_empty_when_nothing_was_answered() -> None:
    assert render_question_answers([QuestionAnswer(question="Unanswered?")]) == ""
    assert render_question_answers([]) == ""
