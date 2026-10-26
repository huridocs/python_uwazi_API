"""The prompt-improvement CHAT: the modal the operator revises a run's prompt in.

The chat opens with the run's ORIGINAL prompt and the validation step's resolved
clarifications (both read-only context), then the operator types free-form
requests. The chat has ONE input action: **Send**. Sending appends the request
as a bubble and triggers the LLM CLARIFICATION step; the clarifying questions
open in their own modal (the same checkbox cards as the New task wizard's
validation step). The operator's answers are folded into the SAME bubble as the
request, in the wizard's ``Clarifications (resolved during validation):``
format, so every bubble has the same shape and the assistant's "thinking" is
never shown.

After the last bubble a single **Run modifications** button appears, and only
when the chat has changes. It regenerates the run's script from the WHOLE
user-and-clarifications conversation and reconciles the result into the ONE
live review table — the rows the new script still produces keep their identity
and status, changed rows go back to pending, rows it no longer produces become
obsolete.

The chat's history lives in this modal's in-memory state. The durable record of
what the prompt became is the manifest's ``validated_prompt`` (rewritten on
every Run) plus the generation history file, so the next turn repairs against
the last one's context.

The improvement flow does NOT open the LLM-spend approval dialog: a
clarification turn is one tool-free request and the Run is the run's normal
planning pass, so both run on the configured default caps without asking the
operator to review caps or estimates.

The modal chrome (the header with its Close, the pinned footer) is reused from
:mod:`uwazi_admin_agent.drivers.web.task_dialogs` rather than re-implemented.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from nicegui import app, background_tasks, context, ui

from uwazi_admin_agent.configuration import (
    PREDICTION_DEFAULT_MAX_COST,
    PREDICTION_DEFAULT_MAX_REQUESTS,
)
from uwazi_admin_agent.domain.llm_budget import LlmBudget
from uwazi_admin_agent.domain.llm_script_usage import apply_script_llm_usage
from uwazi_admin_agent.domain.prompt_chat import (
    ChatMessage,
    ChatRole,
    build_final_prompt_from_chat,
)
from uwazi_admin_agent.domain.prompt_validation import QuestionAnswer, build_final_prompt
from uwazi_admin_agent.drivers.web.cost_dialog import SpendEstimate, build_budget
from uwazi_admin_agent.drivers.web.run_service import (
    clarify_prompt,
    get_run,
    read_run_script,
    regenerate_from_chat,
)
from uwazi_admin_agent.drivers.web.task_dialogs import footer_close, modal_header

# The projected output tokens per regeneration turn (the Run button's planning
# pass). The clarification turns are cheap and run on the configured defaults.
_CHAT_OUT_TOKENS: int = 1024


def _stamp() -> str:
    """The current time as a short display stamp (for the chat messages)."""
    return datetime.now().astimezone().strftime("%H:%M")


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

    # The chat's state: the history (what the generation LLM sees), plus the UI
    # handles the handlers need to touch.
    state: dict[str, Any] = {
        "run_id": run_id,
        "history": [],
        "busy": False,
        "dirty": False,
        "_pending_index": None,
        "_pending_request": None,
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
                    run_area = ui.row().classes("w-full q-mt-sm")
                state["_scroll"] = scroll
                state["_messages"] = messages
                ui.separator().classes("shrink-0")
                with ui.row().classes("w-full items-end no-wrap gap-2 shrink-0"):
                    request_input = (
                        ui.textarea(placeholder="Describe the change you want…")
                        .classes("w-full font-mono")
                        .props("autogrow input-style='min-height: 60px;'")
                    )
                    send_button = ui.button("Send", icon="send", on_click=lambda: _send(state, request_input))
                    state["_send_button"] = send_button
                footer_close(dialog)

            with run_area:
                run_button = ui.button(
                    "Run modifications",
                    icon="play_arrow",
                    on_click=lambda: _run_clicked(state, on_table_changed),
                ).props("color=primary")
            run_button.set_visibility(False)
            state["_run_button"] = run_button

            _load_initial_context(state, detail)

    # Enter sends; Shift+Enter inserts a newline. The SAME handler serves the
    # button and the key, so the two paths cannot drift.
    def _on_enter(event: Any) -> None:
        modifiers = getattr(event.args, "modifiers", None)
        if modifiers is not None and getattr(modifiers, "shift", False):
            return  # Shift+Enter: leave the newline in the textarea
        _send(state, request_input)

    request_input.on("keydown.enter", _on_enter)

    dialog.open()


def _add_prompt_bubble(state: dict[str, Any], text: str, stamp: str) -> Any:
    """Render one prompt bubble in the ONE style every chat bubble uses."""
    with state["_client"]:
        with state["_messages"]:
            return ui.chat_message(text=text, name="Prompt", stamp=stamp, sent=False)


def _load_initial_context(state: dict[str, Any], detail: Any) -> None:
    """Record the run's prompt + existing clarifications and show them as ONE bubble.

    The history keeps the original prompt and the validation message separate
    (that is what :func:`build_final_prompt_from_chat` expects), while the chat
    shows the validation prompt — the original plus its resolved clarifications
    — so the first bubble has the same shape as every later one.
    """
    original = detail.prompt or ""
    validated = detail.validated_prompt or ""
    stamp = _stamp()
    if original:
        state["history"].append(ChatMessage(role=ChatRole.SYSTEM, text=original, stamp=stamp))
    if validated and validated != original:
        state["history"].append(ChatMessage(role=ChatRole.SYSTEM, text=validated, stamp=stamp))
    context_text = validated if validated and validated != original else original
    if context_text:
        _add_prompt_bubble(state, context_text, stamp)


def _send(state: dict[str, Any], request_input: Any) -> None:
    """Send the operator's request: append it, then run the clarification step."""
    if state["busy"]:
        return
    request = (request_input.value or "").strip()
    if not request:
        ui.notify("Type a request first", type="warning")
        return
    request_input.value = ""
    _submit_request(state, request)


def _submit_request(state: dict[str, Any], request: str) -> None:
    """Append one operator request and start the clarification turn.

    The turn runs on the configured default caps: the improve-prompt flow does
    NOT ask for spend approval (neither the caps nor the estimate are shown).
    The clarification sees the WHOLE conversation so far, so it never re-asks
    what an earlier turn already resolved.
    """
    if state["busy"]:
        return
    stamp = _stamp()
    state["history"].append(ChatMessage(role=ChatRole.OPERATOR, text=request, stamp=stamp))
    state["_pending_index"] = len(state["history"]) - 1
    state["_pending_request"] = request
    state["_pending_bubble"] = _add_prompt_bubble(state, request, stamp)
    state["dirty"] = True
    _set_busy(state, True)
    prompt = build_final_prompt_from_chat(state["history"])
    background_tasks.create(_do_clarify(state, prompt), name=f"clarify prompt {state['run_id']}")
    _scroll_to_bottom(state)


async def _do_clarify(state: dict[str, Any], prompt: str) -> None:
    """Run the clarification step and open its questions modal."""
    try:
        understanding = await clarify_prompt(
            prompt,
            app.storage.user["user"],
            app.storage.user["password"],
        )
    except Exception as exc:  # noqa: BLE001 — the chat reports; the page never crashes
        _set_busy(state, False)
        ui.notify(f"Clarification failed: {exc}", type="negative", multi_line=True)
        return
    _set_busy(state, False)
    if understanding.questions:
        _open_questions_dialog(state, understanding.questions)


def _open_questions_dialog(state: dict[str, Any], questions: list[Any]) -> None:
    """Show the clarifications in their own modal.

    The questions use the same checkbox cards as the New task wizard's
    validation step: every suggested option is a checkbox, plus a "Custom
    answer" checkbox and free-text input. Sending the answers folds them into
    the request's existing bubble instead of appending an assistant message.
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
                modal_header(dialog, "Clarifying questions")
                ui.separator().classes("shrink-0")
                with ui.scroll_area().classes("w-full grow"):
                    with ui.column().classes("w-full gap-2"):
                        answer_widgets: list[dict[str, Any]] = []
                        for question in questions:
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
                    ui.button("Skip", on_click=lambda: _skip_questions(state, dialog)).props("color=grey-7 flat")
                    ui.button(
                        "Send answers",
                        icon="send",
                        on_click=lambda: _submit_answers(state, dialog, answer_widgets),
                    ).props("color=primary")
        state["_questions_dialog"] = dialog
    dialog.open()


def _skip_questions(state: dict[str, Any], dialog: Any) -> None:
    """Close the clarifications without folding anything into the request."""
    dialog.close()
    state["_pending_index"] = None
    state["_pending_request"] = None
    _refresh_run_button(state)


def _submit_answers(state: dict[str, Any], dialog: Any, answer_widgets: list[dict[str, Any]]) -> None:
    """Fold the ticked answers into the request's bubble (no new assistant turn)."""
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
    if not any(answer.selected or (answer.custom or "").strip() for answer in answers):
        ui.notify("Tick an option or write an answer first", type="warning")
        return
    request = state.get("_pending_request") or ""
    dialog.close()
    _update_pending_message(state, build_final_prompt(request, answers, ""))
    state["_pending_index"] = None
    state["_pending_request"] = None
    _refresh_run_button(state)


def _update_pending_message(state: dict[str, Any], text: str) -> None:
    """Replace the pending request's bubble (and history message) with ``text``."""
    index = state.get("_pending_index")
    if index is None:
        return
    previous = state["history"][index]
    state["history"][index] = ChatMessage(role=ChatRole.OPERATOR, text=text, stamp=previous.stamp)
    bubble = state.get("_pending_bubble")
    with state["_client"]:
        if bubble is not None and not bubble.is_deleted:
            bubble.delete()
    state["_pending_bubble"] = _add_prompt_bubble(state, text, previous.stamp)
    _scroll_to_bottom(state)


def _set_busy(state: dict[str, Any], busy: bool) -> None:
    """Flip the busy state and refresh the Send / Run affordances."""
    state["busy"] = busy
    _set_send_enabled(state, not busy)
    _refresh_run_button(state)


def _set_send_enabled(state: dict[str, Any], enabled: bool) -> None:
    """Enable or disable the one input action."""
    button = state.get("_send_button")
    if button is not None and not button.is_deleted:
        with state.get("_client"):
            button.enable() if enabled else button.disable()


def _refresh_run_button(state: dict[str, Any]) -> None:
    """Show the Run button only when the chat has changes and nothing is running."""
    button = state.get("_run_button")
    if button is None or button.is_deleted:
        return
    with state.get("_client"):
        button.set_visibility(state["dirty"] and not state["busy"])
        if state["busy"]:
            button.disable()
        else:
            button.enable()


def _scroll_to_bottom(state: dict[str, Any]) -> None:
    """Scroll the chat to the newest message."""
    scroll = state.get("_scroll")
    if scroll is not None and not scroll.is_deleted:
        with state.get("_client"):
            scroll.scroll_to(percent=100, axis="vertical", duration=0.2)


def _run_clicked(state: dict[str, Any], on_table_changed: Callable[[], None] | None) -> None:
    """The Run button: regenerate the script from the whole chat and reconcile."""
    if state["busy"] or not state["dirty"]:
        return
    budget = build_budget(_regeneration_estimate(state["run_id"]))
    _start_run(state, on_table_changed, budget)


def _start_run(
    state: dict[str, Any],
    on_table_changed: Callable[[], None] | None,
    budget: LlmBudget,
) -> None:
    """Regenerate in the background (no spend confirmation)."""
    _set_busy(state, True)
    background_tasks.create(
        _do_run(state, on_table_changed, budget),
        name=f"chat regeneration {state['run_id']}",
    )


async def _do_run(
    state: dict[str, Any],
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
        _set_busy(state, False)
        ui.notify(f"Regeneration failed: {exc}", type="negative", multi_line=True)
        return
    state["dirty"] = False
    _set_busy(state, False)
    with state.get("_client"):
        ui.notify(result.message(), type="positive", multi_line=True)
    if on_table_changed is not None:
        on_table_changed()


__all__ = ["prompt_chat_dialog"]
