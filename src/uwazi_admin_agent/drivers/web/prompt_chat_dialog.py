"""The prompt-improvement CHAT: the modal the operator revises a run's prompt in.

The read-only prompt modal is gone; this is what replaced it. The chat opens
with the run's ORIGINAL prompt and the validation step's resolved
clarifications (both read-only context), then the operator types free-form
requests. Every turn the assistant answers with what it understood, the
clarifying questions it still has, and the COMPLETE revised prompt it
proposes.

The **Run** button is the point of the whole feature: it regenerates the run's
script from the WHOLE chat history and reconciles the result into the ONE
live review table — the rows the new script still produces keep their identity
and status, changed rows go back to pending, rows it no longer produces become
obsolete.

The chat's history lives in this modal's in-memory state. The durable record of
what the prompt became is the manifest's ``validated_prompt`` (rewritten on
every Run) plus the generation history file, so the next turn repairs against
the last one's context.

The improve-prompt flow does NOT open the LLM-spend approval dialog: a turn is
one tool-free request and the Run is the run's normal planning pass, so both
run on the configured default caps without asking the operator to review caps
or estimates.

When a turn carries clarifying questions they open in their own "Review
questions" modal, with the same checkbox cards as the New task wizard's
validation step. Sending the answers turns them into the operator's next chat
request.

The modal chrome (the header with its Close, the pinned footer) is reused from
:mod:`uwazi_admin_agent.drivers.web.task_dialogs` rather than re-implemented.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from nicegui import app, background_tasks, context, ui

from uwazi_admin_agent.configuration import (
    IMPROVE_DEFAULT_MAX_COST,
    IMPROVE_DEFAULT_MAX_REQUESTS,
    PREDICTION_DEFAULT_MAX_COST,
    PREDICTION_DEFAULT_MAX_REQUESTS,
)
from uwazi_admin_agent.domain.llm_budget import LlmBudget, estimate_tokens
from uwazi_admin_agent.domain.llm_script_usage import apply_script_llm_usage
from uwazi_admin_agent.domain.prompt_chat import (
    ChatMessage,
    ChatRole,
    ChatTurn,
    build_chat_history,
    render_question_answers,
)
from uwazi_admin_agent.domain.prompt_validation import QuestionAnswer
from uwazi_admin_agent.drivers.web.cost_dialog import SpendEstimate, build_budget
from uwazi_admin_agent.drivers.web.run_service import (
    get_run,
    improve_prompt,
    read_run_script,
    regenerate_from_chat,
)
from uwazi_admin_agent.drivers.web.task_dialogs import footer_close, modal_header

# The assistant's answer arrives as ONE message; the clarifying questions open
# in their own modal, and the revised prompt is rendered under the message. The
# projected output tokens per chat turn.
_CHAT_OUT_TOKENS: int = 1024


def _stamp() -> str:
    """The current time as a short display stamp (for the chat messages)."""
    return datetime.now().astimezone().strftime("%H:%M")


def _chat_estimate(history: list[ChatMessage], request: str) -> SpendEstimate:
    """The silent budget source for ONE chat turn (the dialog never shows it).

    The improve-prompt flow no longer asks the operator to approve caps or
    review estimates: the configured defaults are used as the enforced limits.
    """
    history_text = build_chat_history(history)
    return SpendEstimate(
        title="Improve prompt",
        detail="One assistant turn over this conversation.",
        n_calls=1,
        n_entities=0,
        est_in_tokens_per_call=estimate_tokens(history_text) + estimate_tokens(request),
        est_out_tokens_per_call=_CHAT_OUT_TOKENS,
        default_max_cost=IMPROVE_DEFAULT_MAX_COST,
        default_max_requests=IMPROVE_DEFAULT_MAX_REQUESTS,
        notes=[
            "The request cap is the enforced limit for this turn: the turn is a single tool-free LLM call.",
        ],
    )


def _regeneration_estimate(run_id: str) -> SpendEstimate:
    """The silent budget source for the Run button's regeneration + planning pass.

    The projection is the CURRENT script's own LLM call sites (the planning
    pass runs the script once) plus the generation call itself. The entity
    count is unknown until the pass runs.
    """
    try:
        script = read_run_script(run_id)
    except OSError:
        script = ""
    usage = apply_script_llm_usage(script)
    return SpendEstimate(
        title="Regenerate and update the table",
        detail="Regenerate the script from the chat, then re-plan it over the whole table.",
        n_calls=usage.calls_per_entity + 1,
        n_entities=0,
        est_in_tokens_per_call=0,
        est_out_tokens_per_call=_CHAT_OUT_TOKENS,
        default_max_cost=PREDICTION_DEFAULT_MAX_COST,
        default_max_requests=PREDICTION_DEFAULT_MAX_REQUESTS,
        notes=[
            "The request count is a projection: the script's own LLM calls are per entity, "
            "and the entity count is only known once the pass runs.",
        ],
    )


def prompt_chat_dialog(run_id: str, on_table_changed: Callable[[], None] | None = None) -> None:
    """Open the prompt-improvement chat for ``run_id``.

    ``on_table_changed`` is called after the chat's Run button rewrote the
    review table, so the page behind the modal can repaint it. It is optional:
    the runs table opens the modal without one.
    """
    try:
        detail = get_run(run_id)
    except Exception as exc:  # noqa: BLE001
        ui.notify(f"Failed to load run: {exc}", type="negative", multi_line=True)
        return

    # The chat's state: the history (what the assistant sees), plus the UI
    # handles the handlers need to touch.
    state: dict[str, Any] = {
        "run_id": run_id,
        "history": [],
        "last_turn": None,
        "busy": False,
    }

    client = context.client
    state["_client"] = client

    with context.client.layout:
        with ui.dialog() as dialog, ui.card().classes("w-full max-w-3xl").props("style=height:85dvh"):
            with ui.column().classes("w-full h-full items-stretch no-wrap"):
                modal_header(dialog, f"Prompt — {run_id}")
                ui.separator().classes("shrink-0")
                scroll = ui.scroll_area().classes("w-full grow")
                with scroll:
                    messages = ui.column().classes("w-full gap-2")
                state["_scroll"] = scroll
                state["_messages"] = messages
                ui.separator().classes("shrink-0")
                with ui.row().classes("w-full items-end no-wrap gap-2 shrink-0"):
                    request_input = (
                        ui.textarea(placeholder="Describe the change you want…")
                        .classes("w-full font-mono")
                        .props("autogrow input-style='min-height: 60px;'")
                    )
                    ui.button("Send", icon="send", on_click=lambda: _send(state, request_input))
                    run_button = ui.button(
                        "Run",
                        icon="play_arrow",
                        on_click=lambda: _run_clicked(state, dialog, on_table_changed),
                    )
                    run_button.disable()
                    state["_run_button"] = run_button
                footer_close(dialog)

            # The opening context: the original prompt, then the validation
            # step's clarifications (only when the run WAS validated and the
            # validated prompt differs from the original). Both are read-only
            # context, rendered inside the modal's own layout.
            original = detail.prompt or ""
            if original:
                _add_system_message(state, messages, original, "Original prompt")
            validated = detail.validated_prompt or ""
            if validated and validated != original:
                _add_system_message(state, messages, validated, "Validation")

    # Enter sends; Shift+Enter inserts a newline. The SAME handler serves the
    # button and the key, so the two paths cannot drift.
    def _on_enter(event: Any) -> None:
        modifiers = getattr(event.args, "modifiers", None)
        if modifiers is not None and getattr(modifiers, "shift", False):
            return  # Shift+Enter: leave the newline in the textarea
        _send(state, request_input)

    request_input.on("keydown.enter", _on_enter)

    dialog.open()


def _add_system_message(state: dict[str, Any], messages: Any, text: str, name: str) -> None:
    """Render one read-only context message and record it in the history."""
    message = ChatMessage(role=ChatRole.SYSTEM, text=text, stamp=_stamp())
    state["history"].append(message)
    with messages:
        ui.chat_message(text=text, name=name, stamp=message.stamp, sent=False)


def _send(state: dict[str, Any], request_input: Any) -> None:
    """Send the operator's request: append it, then ask the assistant."""
    if state["busy"]:
        return
    request = (request_input.value or "").strip()
    if not request:
        ui.notify("Type a request first", type="warning")
        return
    request_input.value = ""
    _submit_request(state, request)


def _submit_request(state: dict[str, Any], request: str) -> None:
    """Append one operator request and start the assistant's turn.

    The turn runs on the configured default caps: the improve-prompt flow does
    NOT ask for spend approval (neither the caps nor the estimate are shown).
    """
    if state["busy"]:
        return
    messages = state["_messages"]
    client = state.get("_client")
    message = ChatMessage(role=ChatRole.OPERATOR, text=request, stamp=_stamp())
    state["history"].append(message)
    with client:
        with messages:
            state["_operator_message"] = ui.chat_message(text=request, name="You", stamp=message.stamp, sent=True)
            # The pending assistant message: its text is replaced by the answer.
            state["_pending"] = ui.chat_message(text="Thinking…", name="Assistant", stamp=_stamp(), sent=False)

    budget = build_budget(_chat_estimate(state["history"][:-1], request))
    _start_turn(state, request, budget)


def _start_turn(state: dict[str, Any], request: str, budget: LlmBudget) -> None:
    """Run the turn in the background."""
    state["busy"] = True
    _set_run_enabled(state, False)
    background_tasks.create(_do_turn(state, request, budget), name=f"chat turn {state['run_id']}")


async def _do_turn(state: dict[str, Any], request: str, budget: LlmBudget) -> None:
    """Run one chat turn and render the assistant's answer."""
    try:
        turn = await improve_prompt(
            state["run_id"],
            state["history"][:-1],
            request,
            app.storage.user["user"],
            app.storage.user["password"],
            budget,
        )
    except Exception as exc:  # noqa: BLE001 — the chat reports; the page never crashes
        _render_turn_error(state, exc)
        return
    _render_turn(state, turn)


def _render_turn(state: dict[str, Any], turn: ChatTurn) -> None:
    """Replace the pending message with the assistant's answer."""
    state["busy"] = False
    client = state.get("_client")
    state["last_turn"] = turn
    message = ChatMessage(
        role=ChatRole.ASSISTANT,
        text=turn.summary,
        stamp=_stamp(),
        questions=list(turn.questions),
        revised_prompt=turn.revised_prompt,
    )
    state["history"].append(message)

    pending = state.get("_pending")
    messages = state["_messages"]
    with client:
        if pending is not None and not pending.is_deleted:
            pending.delete()
        with messages:
            assistant_message = ui.chat_message(text=turn.summary, name="Assistant", stamp=message.stamp, sent=False)
            if turn.questions:
                with assistant_message:
                    ui.button(
                        f"Answer {len(turn.questions)} question{'' if len(turn.questions) == 1 else 's'}",
                        icon="help_outline",
                        on_click=lambda: _open_questions_dialog(state, turn),
                    ).props("dense flat")
            if turn.revised_prompt:
                ui.textarea(value=turn.revised_prompt).classes("w-full font-mono").props("readonly outlined autogrow")
    _set_run_enabled(state, True)
    state.pop("_pending", None)
    _scroll_to_bottom(state)
    if turn.questions:
        _open_questions_dialog(state, turn)


def _open_questions_dialog(state: dict[str, Any], turn: ChatTurn) -> None:
    """Show the assistant's clarifying questions in their own modal.

    The questions use the same checkbox cards as the New task wizard's
    validation step: every suggested option is a checkbox, plus a "Custom
    answer" checkbox and free-text input. Sending the answers turns them into
    the operator's next chat request.
    """
    client = state.get("_client")
    with client.layout:
        previous = state.get("_questions_dialog")
        if previous is not None and not previous.is_deleted:
            previous.close()
            previous.delete()

        with ui.dialog() as dialog, ui.card().classes("w-full max-w-3xl").props("style=height:85dvh"):
            dialog.props("persistent")
            with ui.column().classes("w-full h-full items-stretch no-wrap"):
                modal_header(dialog, "Review questions")
                ui.separator().classes("shrink-0")
                with ui.scroll_area().classes("w-full grow"):
                    with ui.column().classes("w-full gap-2"):
                        ui.label("What the LLM understood").classes("text-subtitle1 text-grey-7")
                        ui.label(turn.summary).classes("text-body1 q-mb-md")
                        ui.label("Clarifying questions").classes("text-subtitle1 text-grey-7")
                        answer_widgets: list[dict[str, Any]] = []
                        for question in turn.questions:
                            with ui.card().classes("w-full q-mb-sm"):
                                ui.label(question.question).classes("text-body1 text-weight-medium")
                                option_checkboxes = [ui.checkbox(option) for option in question.options]
                                custom_checkbox = ui.checkbox("Custom answer")
                                custom_input = ui.input("Your answer").classes("w-full")
                                answer_widgets.append(
                                    {
                                        "question": question.question,
                                        "options": list(zip(question.options, option_checkboxes)),
                                        "custom_checkbox": custom_checkbox,
                                        "custom_input": custom_input,
                                    }
                                )
                ui.separator().classes("shrink-0")
                with ui.row().classes("w-full justify-end no-wrap shrink-0"):
                    ui.button("Skip", on_click=dialog.close).props("color=grey-7 flat")
                    ui.button(
                        "Send answers",
                        icon="send",
                        on_click=lambda: _submit_answers(state, dialog, answer_widgets),
                    ).props("color=primary")
        state["_questions_dialog"] = dialog
    dialog.open()


def _submit_answers(state: dict[str, Any], dialog: Any, answer_widgets: list[dict[str, Any]]) -> None:
    """Turn the ticked answers into the operator's next chat request."""
    answers = [
        QuestionAnswer(
            question=widget["question"],
            selected=[option for option, checkbox in widget["options"] if checkbox.value],
            custom=(
                widget["custom_input"].value.strip()
                if widget["custom_checkbox"].value and (widget["custom_input"].value or "").strip()
                else None
            ),
        )
        for widget in answer_widgets
    ]
    request = render_question_answers(answers)
    if not request:
        ui.notify("Tick an option or write an answer first", type="warning")
        return
    if state["busy"]:
        ui.notify("Wait for the current turn to finish", type="warning")
        return
    dialog.close()
    _submit_request(state, request)


def _render_turn_error(state: dict[str, Any], exc: Exception) -> None:
    """Show a failed turn in the chat (the modal stays open)."""
    state["busy"] = False
    client = state.get("_client")
    pending = state.get("_pending")
    with client:
        if pending is not None and not pending.is_deleted:
            pending.delete()
        with state["_messages"]:
            ui.chat_message(text=f"Chat turn failed: {exc}", name="Assistant", sent=False).classes("text-negative")
    _set_run_enabled(state, state.get("last_turn") is not None)
    state.pop("_pending", None)
    ui.notify(f"Chat turn failed: {exc}", type="negative", multi_line=True)


def _set_run_enabled(state: dict[str, Any], enabled: bool) -> None:
    """Enable the Run button only when a revised prompt is ready."""
    button = state.get("_run_button")
    if button is not None and not button.is_deleted:
        with state.get("_client"):
            button.enable() if enabled else button.disable()


def _scroll_to_bottom(state: dict[str, Any]) -> None:
    """Scroll the chat to the newest message."""
    scroll = state.get("_scroll")
    if scroll is not None and not scroll.is_deleted:
        with state.get("_client"):
            scroll.scroll_to(percent=100, axis="vertical", duration=0.2)


def _run_clicked(state: dict[str, Any], dialog: Any, on_table_changed: Callable[[], None] | None) -> None:
    """The Run button: regenerate the script from the whole chat and reconcile."""
    if state["busy"]:
        return
    turn = state.get("last_turn")
    if turn is None or not turn.revised_prompt:
        ui.notify("Send a request first so the assistant can revise the prompt", type="warning")
        return
    budget = build_budget(_regeneration_estimate(state["run_id"]))
    _start_run(state, dialog, on_table_changed, budget)


def _start_run(
    state: dict[str, Any],
    dialog: Any,
    on_table_changed: Callable[[], None] | None,
    budget: LlmBudget,
) -> None:
    """Regenerate in the background (no spend confirmation)."""
    state["busy"] = True
    _set_run_enabled(state, False)
    background_tasks.create(
        _do_run(state, dialog, on_table_changed, budget),
        name=f"chat regeneration {state['run_id']}",
    )


async def _do_run(
    state: dict[str, Any],
    dialog: Any,
    on_table_changed: Callable[[], None] | None,
    budget: LlmBudget,
) -> None:
    """Regenerate the script from the chat and reconcile the live table."""
    try:
        result = await regenerate_from_chat(
            state["run_id"],
            state["history"],
            app.storage.user["user"],
            app.storage.user["password"],
            budget,
        )
    except Exception as exc:  # noqa: BLE001 — the chat reports; the modal stays open
        state["busy"] = False
        _set_run_enabled(state, state.get("last_turn") is not None)
        ui.notify(f"Regeneration failed: {exc}", type="negative", multi_line=True)
        return
    state["busy"] = False
    with state.get("_client"):
        ui.notify(result.message(), type="positive", multi_line=True)
        dialog.close()
    if on_table_changed is not None:
        on_table_changed()


__all__ = ["prompt_chat_dialog"]
