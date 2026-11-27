"""The prompt-improvement CHAT: the modal the operator revises a run's prompt in.

The chat opens with the run's ORIGINAL prompt and the validation step's resolved
clarifications (both read-only context), then the operator types free-form
requests. The chat has ONE input action: **Send**. Sending appends the request
as its OWN bubble and triggers the LLM CLARIFICATION step; the clarifying
questions open in their own modal (the same checkbox cards as the New task
wizard's validation step). The operator's answers are folded into the SAME
bubble as that request, in the wizard's ``Clarifications (resolved during
validation):`` format, so every bubble has the same shape. The clarification
step's restatement ("Understanding") and its questions are shown as context
bubbles AND recorded in the history, so the generation LLM receives them too —
a change request read as a report is visible before the Run.

Every prompt bubble carries a status badge: **Run** once the prompt is part of
the script's current prompt, **Not run yet** while it waits for the Run button.
The base prompt (original + clarifications) and each prior request recovered
from the persisted prompt get their own bubble, so re-opening the chat never
folds several prompts into one block. Every bubble is painted with the modal's
own light-blue background (``rgb(198, 220, 236)``), scoped to a
``prompt-bubble`` class so Quasar's default chat colors are untouched elsewhere.

After the last bubble a single **Run modifications** button appears, and only
when the chat has changes. It regenerates the run's script from the WHOLE
user-and-clarifications conversation and reconciles the result into the ONE
live review table — the rows the new script still produces keep their identity
and status, changed rows go back to pending, and rows it no longer produces are
deleted (only an APPLIED write stays, as the run's history). While that runs, the
modal shows a spinner with the backend's current
stage (regenerating the script, saving it, recomputing the table) and a start
toast; the closing toast reports the reconciliation counts.

The chat's history lives in this modal's in-memory state. The durable record of
what the prompt became is the manifest's ``validated_prompt`` — the WHOLE folded
context (original prompt + validation clarifications + every request with its
own answers + every understanding/turn) — rewritten on every Run and rebuilt
into bubbles when the chat is reopened. Each Run generates a FRESH script from
that context with the CURRENT system prompt; no old generation conversation is
replayed.

The improvement flow does NOT open the LLM-spend approval dialog: a
clarification turn is one tool-free request and the Run is the run's normal
planning pass, so both run on the configured default caps without asking the
operator to review caps or estimates.

The modal also serves the entity window's **Fix the prompt** action. Each click
adds a :class:`SuggestionCorrection` — the entity, property, Uwazi's current
value and the wrong proposed value — to the chat's PENDING FIXES, so the
operator can flag several wrong rows, reopening the chat for each. One "Add all
fixes to the prompt" folds them into a single request
(``combined_correction_request``), which continues through the SAME
clarification/questions/Run path. The Run ALWAYS sends the run's current script
alongside the prompt, so the generator revises the script the operator already
reviewed (with the same authoring and benchmark tools available) instead of
recreating it from the prompt alone.

The modal chrome (the header with its top-right Close) is reused from
:mod:`uwazi_admin_agent.drivers.web.task_dialogs` rather than re-implemented.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from nicegui import background_tasks, context, ui

from uwazi_admin_agent.configuration import (
    PREDICTION_DEFAULT_MAX_COST,
    PREDICTION_DEFAULT_MAX_REQUESTS,
)
from uwazi_admin_agent.domain.llm_budget import LlmBudget
from uwazi_admin_agent.domain.llm_script_usage import apply_script_llm_usage
from uwazi_admin_agent.domain.prompt_chat import (
    ChatMessage,
    ChatPromptStatus,
    ChatRole,
    apply_compaction,
    build_compaction_input,
    build_final_prompt_from_chat,
    mark_prompts_applied,
    reconstruct_chat,
)
from uwazi_admin_agent.domain.prompt_revision import (
    SuggestionCorrection,
    combined_correction_request,
    correction_summary,
    merge_corrections,
)
from uwazi_admin_agent.domain.prompt_validation import QuestionAnswer, build_final_prompt
from uwazi_admin_agent.drivers.web.cost_dialog import SpendEstimate, build_budget
from uwazi_admin_agent.drivers.web.operation_gate import (
    release_run,
    run_claim,
    run_claim_text,
    set_claim_note,
    try_claim_run,
)
from uwazi_admin_agent.drivers.web.run_service import (
    clarify_prompt,
    compact_chat,
    get_run,
    read_run_script,
    regenerate_from_chat,
)
from uwazi_admin_agent.drivers.web.session import credentials_for_instance, page_instance_url
from uwazi_admin_agent.drivers.web.task_dialogs import modal_header

# The projected output tokens per regeneration turn (the Run button's planning
# pass). The clarification turns are cheap and run on the configured defaults.
_CHAT_OUT_TOKENS: int = 1024

# The status badge on every prompt bubble. "Applied" means the prompt is part of
# the script's current prompt (it was written); "Not applied yet" means the
# operator wrote it and has not pressed Apply since.
_PROMPT_STATUS_TEXT: dict[ChatPromptStatus, str] = {
    ChatPromptStatus.APPLIED: "Applied",
    ChatPromptStatus.PENDING: "Not applied yet",
}
_PROMPT_STATUS_COLOR: dict[ChatPromptStatus, str] = {
    ChatPromptStatus.APPLIED: "green",
    ChatPromptStatus.PENDING: "orange",
}

# Every prompt bubble carries this class so the modal paints its background
# without touching Quasar's own chat colors. Quasar fills a received bubble with
# ``background: currentColor`` and sets that color on ``.q-message-text--received``,
# so overriding the color there (leaving the inner content black) is what
# changes the bubble background.
PROMPT_BUBBLE_CLASS = "prompt-bubble"
PROMPT_BUBBLE_BACKGROUND = "var(--uw-carbon-tint)"
_PROMPT_BUBBLE_CSS = (
    f"<style>.{PROMPT_BUBBLE_CLASS} .q-message-text--received {{ color: {PROMPT_BUBBLE_BACKGROUND} !important; }}</style>"
)


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
        script = read_run_script(page_instance_url(), run_id)
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


def _notify_instance(instance: str, message: str, type_: str = "positive", **kwargs: Any) -> None:
    """Toast every client of ``instance`` (background-safe, dead clients skipped).

    The Run's background task outlives the modal: when the page navigates to
    the tasks table the dialog's client is deleted, so a ``ui.notify`` against
    it would warn ("Client has been deleted"). The instance broadcast reaches
    whichever page is still open instead.
    """
    if not instance:
        return
    # Local import: the app package imports this module, so a top-level import
    # would close the cycle.
    from uwazi_admin_agent.drivers.web.app.state import _broadcast_notify

    _broadcast_notify(instance, message, type=type_, **kwargs)


def _broadcast_run_rows(instance: str) -> None:
    """Push fresh task rows so the run's ``regenerating`` badge appears at once."""
    if not instance:
        return
    from uwazi_admin_agent.drivers.web.app.broadcast import _broadcast_rows

    _broadcast_rows(instance)


def prompt_chat_dialog(
    run_id: str,
    on_table_changed: Callable[[], None] | None = None,
    *,
    correction: SuggestionCorrection | None = None,
    on_run_started: Callable[[], None] | None = None,
) -> None:
    """Open the prompt-improvement chat for ``run_id``.

    ``on_table_changed`` is called after the chat's Run button rewrote the
    review table, so the page behind the modal can repaint it. It is optional:
    the runs table opens the modal without one.

    ``correction`` opens the modal in CORRECTION mode, from the entity window's
    "Fix the prompt" action: the flagged row joins the chat's pending fixes.
    The operator can open the chat again for ANOTHER wrong row; each flag is
    added to the same list, and one "Add all fixes to the prompt" turns them
    into one request, which the Run regenerates with the current script in
    hand (and the same authoring/benchmark tools the first turn had).

    ``on_run_started`` is called right after the regeneration is claimed and
    launched. The review page uses it to move to the tasks window, where the
    run's row shows the ``regenerating`` state while the script is rebuilt.
    """
    try:
        detail = get_run(page_instance_url(), run_id)
    except Exception as exc:  # noqa: BLE001
        ui.notify(f"Failed to load run: {exc}", type="negative", multi_line=True)
        return

    # The bubble color: injected with the dialog (not the page) so both pages
    # that can open the chat get it, and an already-built page is updated too.
    ui.add_head_html(_PROMPT_BUBBLE_CSS)

    # The chat's state: the history (what the generation LLM sees), plus the UI
    # handles the handlers need to touch. ``_unrun_bubbles`` holds the prompt
    # bubbles written in THIS session that a successful Run has not applied yet.
    state: dict[str, Any] = {
        "run_id": run_id,
        "history": [],
        "busy": False,
        "dirty": False,
        # The wrong rows the operator flagged for THIS run. Each "Fix the
        # prompt" click ADDS one; nothing is sent until the operator presses
        # "Add all fixes to the prompt", which turns them into ONE request so a
        # single regeneration fixes them all. The list lives on the browser
        # client (not this dialog), because flagging a second row re-opens the
        # chat: a dialog-scoped list would lose the first fix.
        "corrections": [],
        "_correction_cards": [],
        "_pending_index": None,
        "_pending_request": None,
        "_pending_bubble": None,
        "_unrun_bubbles": [],
        "_run_spinner": None,
        "_run_status": None,
    }

    client = context.client
    state["_client"] = client

    with context.client.layout:
        with ui.dialog() as dialog, ui.card().classes("w-full max-w-3xl uw-dialog").props("style=height:85dvh"):
            with ui.column().classes("w-full h-full items-stretch no-wrap"):
                modal_header(dialog, f"Fix prompt — {run_id}" if correction is not None else f"Prompt — {run_id}")
                scroll = ui.scroll_area().classes("w-full grow")
                with scroll:
                    messages = ui.column().classes("w-full gap-2")
                    run_area = ui.row().classes("w-full q-mt-sm")
                state["_scroll"] = scroll
                state["_messages"] = messages
                with ui.row().classes("w-full items-end no-wrap gap-2 shrink-0 uw-chat-panel"):
                    request_input = (
                        ui.textarea(placeholder="Describe the change you want…")
                        .classes("w-full font-mono")
                        .props("outlined autogrow input-style='min-height: 60px;'")
                    )
                    send_button = ui.button("Send", icon="send", on_click=lambda: _send(state, request_input)).props(
                        "unelevated no-caps color=primary"
                    )
                    state["_send_button"] = send_button
                    compact_button = (
                        ui.button(
                            "Compact",
                            icon="compress",
                            on_click=lambda: _compact_clicked(state),
                        )
                        .props("flat dense no-caps")
                        .classes("uw-btn-ghost")
                    )
                    state["_compact_button"] = compact_button

            with run_area:
                spinner = ui.spinner(size="sm", color="primary")
                status_label = ui.label("").classes("text-caption text-grey-7")
                run_button = ui.button(
                    "Apply modifications",
                    icon="play_arrow",
                    on_click=lambda: _run_clicked(state, on_table_changed, on_run_started),
                ).props("unelevated no-caps color=primary")
            run_button.set_visibility(False)
            spinner.set_visibility(False)
            state["_run_button"] = run_button
            state["_run_spinner"] = spinner
            state["_run_status"] = status_label

            _load_initial_context(state, detail)
            state["corrections"] = list(_pending_corrections(client, run_id))
            if correction is not None:
                _add_correction(state, correction)
            elif state["corrections"]:
                _render_corrections_card(state)

    # Enter sends; Shift+Enter inserts a newline. The SAME handler serves the
    # button and the key, so the two paths cannot drift.
    def _on_enter(event: Any) -> None:
        modifiers = getattr(event.args, "modifiers", None)
        if modifiers is not None and getattr(modifiers, "shift", False):
            return  # Shift+Enter: leave the newline in the textarea
        _send(state, request_input)

    request_input.on("keydown.enter", _on_enter)

    dialog.open()


def _add_prompt_bubble(
    state: dict[str, Any],
    text: str,
    stamp: str,
    *,
    label: str = "Prompt",
    status: ChatPromptStatus = ChatPromptStatus.PENDING,
) -> dict[str, Any]:
    """Render ONE prompt bubble with its own run-status badge.

    One bubble per prompt (never one bubble holding the whole conversation), so
    the operator can point at the exact request they wrote. The badge says
    whether that prompt is already part of the script ("Applied") or is still
    waiting for the Run button ("Not run yet"). The returned handle lets a
    successful Run flip the badge in place.
    """
    with state["_client"]:
        with state["_messages"]:
            with ui.column().classes("w-full gap-0") as root:
                with ui.row().classes("items-center gap-2 q-ml-sm no-wrap"):
                    ui.label(label).classes("text-caption text-weight-medium")
                    badge = ui.badge(
                        _PROMPT_STATUS_TEXT[status],
                        color=_PROMPT_STATUS_COLOR[status],
                    ).props("rounded")
                    if stamp:
                        ui.label(stamp).classes("text-caption text-grey-6")
                ui.chat_message(text=text, sent=False).classes(PROMPT_BUBBLE_CLASS)
    return {"root": root, "status": badge, "status_value": status}


def _set_bubble_status(state: dict[str, Any], handle: dict[str, Any], status: ChatPromptStatus) -> None:
    """Flip one prompt bubble's run-status badge (in place)."""
    handle["status_value"] = status
    badge = handle.get("status")
    if badge is not None and not badge.is_deleted:
        with state["_client"]:
            badge.set_text(_PROMPT_STATUS_TEXT[status])
            badge.set_background_color(_PROMPT_STATUS_COLOR[status])


def _delete_bubble(state: dict[str, Any], handle: dict[str, Any] | None) -> None:
    """Remove one prompt bubble (used when its text is replaced)."""
    root = handle.get("root") if handle else None
    if root is not None and not root.is_deleted:
        with state["_client"]:
            root.delete()


def _initial_bubbles(
    baseline: str,
    history: list[ChatMessage],
) -> list[tuple[str, str, str, ChatPromptStatus]]:
    """The bubbles a re-opened chat renders, in display order (pure).

    Returns one ``(label, text, stamp, status)`` per bubble: the base prompt
    first, then EVERY prior message in chat order — each operator request and
    each clarification "Understanding" the assistant recorded, so the reopened
    chat shows the same context the generation LLM receives. Keeping this pure
    lets the test prove the recovered history is not collapsed into one bubble.
    """
    bubbles: list[tuple[str, str, str, ChatPromptStatus]] = []
    if baseline:
        bubbles.append(("Base prompt", baseline, "", ChatPromptStatus.APPLIED))
    for message in history:
        if message.role is ChatRole.OPERATOR:
            bubbles.append(("Prompt", message.text, message.stamp, message.status))
        elif message.role is ChatRole.ASSISTANT and (message.text.strip() or message.questions):
            bubbles.append(("Understanding", message.text, message.stamp, ChatPromptStatus.APPLIED))
    return bubbles


def _load_initial_context(state: dict[str, Any], detail: Any) -> None:
    """Record the run's prompt + clarifications and show every prompt in its OWN bubble.

    The history keeps the original prompt and the validation message separate
    (that is what :func:`build_final_prompt_from_chat` expects), while the chat
    shows:

    - ONE "Base prompt" bubble for the run itself (the original plus its
      resolved clarifications), marked Run, and
    - one bubble per prior message recovered from the persisted
      ``validated_prompt``'s conversation: each operator request AND each
      clarification "Understanding", each marked Run.

    Reconstructing the bubbles (instead of rendering the whole validated prompt
    as one block) is what stops a re-opened chat from folding the earlier
    requests into the base prompt, and it keeps the visible context identical to
    the one the generation LLM receives.
    """
    original = detail.prompt or ""
    validated = detail.validated_prompt or ""
    baseline, history = reconstruct_chat(original, validated)
    state["history"].extend(history)
    for label, text, stamp, status in _initial_bubbles(baseline, history):
        _add_prompt_bubble(state, text, stamp, label=label, status=status)


def _pending_corrections(client: Any, run_id: str) -> list[SuggestionCorrection]:
    """The fixes already flagged for ``run_id`` in this browser session (pure store read)."""
    store = getattr(client, "_pending_corrections", None)
    if not isinstance(store, dict):
        return []
    return list(store.get(run_id, []))


def _store_pending_corrections(client: Any, run_id: str, corrections: list[SuggestionCorrection]) -> None:
    """Persist the run's pending fixes on the browser client (survives dialog reopens)."""
    store = getattr(client, "_pending_corrections", None)
    if not isinstance(store, dict):
        store = {}
        client._pending_corrections = store  # noqa: SLF001 — per-client dialog state
    if corrections:
        store[run_id] = list(corrections)
    else:
        store.pop(run_id, None)


def _add_correction(state: dict[str, Any], correction: SuggestionCorrection) -> None:
    """Append one flagged row to the chat's pending fixes (the accumulation step).

    The entity window's "Fix the prompt" can be used again for another wrong
    row: every click lands here. A row flagged twice stays ONE fix (the latest
    explanation wins), so the regeneration request has no duplicate sections.
    The list is rendered as an attachment above the input, with one button that
    folds them ALL into a single request.
    """
    state["corrections"] = merge_corrections(state.get("corrections", []), correction)
    _store_pending_corrections(state["_client"], state["run_id"], state["corrections"])
    _render_corrections_card(state)


def _render_corrections_card(state: dict[str, Any]) -> None:
    """(Re)draw the pending-fixes panel below the conversation."""
    client = state["_client"]
    for card in state.get("_correction_cards", []):
        if card is not None and not card.is_deleted:
            with client:
                card.delete()
    state["_correction_cards"] = []
    corrections: list[SuggestionCorrection] = state.get("corrections", [])
    if not corrections:
        return
    with client:
        with state["_messages"]:
            with ui.card().classes("w-full uw-callout-warning") as card:
                ui.label(f"Fixes to add to the prompt ({len(corrections)})").classes("text-subtitle2 text-weight-medium")
                ui.label(
                    "Flag as many wrong suggestions as you need, then add them all to the "
                    "prompt at once: one Apply regenerates the script and fixes every one."
                ).classes("text-caption text-grey-8 w-full")
                for correction in corrections:
                    ui.label(correction_summary(correction)).classes("text-caption text-grey-8 w-full").style(
                        "white-space: pre-wrap"
                    )
                explanation = (
                    ui.textarea(label="Why are these suggestions wrong? (optional, applies to all)")
                    .props("outlined autogrow")
                    .classes("w-full")
                )
                with ui.row().classes("w-full justify-end"):
                    ui.button(
                        "Add all fixes to the prompt",
                        icon="construction",
                        on_click=lambda: _send_corrections(state, explanation, card),
                    ).props("unelevated no-caps color=negative")
    state["_correction_cards"].append(card)


def _send_corrections(
    state: dict[str, Any],
    explanation_input: Any,
    card: Any,
) -> None:
    """Fold every pending fix into ONE request and start the chat turn.

    The request then follows the SAME path as a typed request (clarification →
    questions → Run), so correction mode adds an attachment, not a second
    conversation. The attachment is removed once it was sent: its text is now
    the request's bubble, and re-sending it would duplicate it.
    """
    if state["busy"]:
        return
    corrections: list[SuggestionCorrection] = state.get("corrections", [])
    if not corrections:
        return
    reason = (explanation_input.value or "").strip()
    if reason:
        corrections = [correction.model_copy(update={"reason": correction.reason or reason}) for correction in corrections]
    request = combined_correction_request(corrections)
    with state["_client"]:
        if not card.is_deleted:
            card.delete()
    state["corrections"] = []
    state["_correction_cards"] = []
    _store_pending_corrections(state["_client"], state["run_id"], [])
    _submit_request(state, request)


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

    The clarification is fed the WHOLE folded context so far —
    ``build_final_prompt_from_chat``: the original prompt, the validation
    clarifications, every earlier request with its answers and every earlier
    understanding — so it neither re-asks what was resolved nor loses the run's
    original intent. The turn runs on the configured default caps: this flow
    does NOT ask for spend approval.
    """
    if state["busy"]:
        return
    stamp = _stamp()
    state["history"].append(ChatMessage(role=ChatRole.OPERATOR, text=request, stamp=stamp))
    state["_pending_index"] = len(state["history"]) - 1
    state["_pending_request"] = request
    handle = _add_prompt_bubble(state, request, stamp, status=ChatPromptStatus.PENDING)
    state["_pending_bubble"] = handle
    state["_unrun_bubbles"].append(handle)
    state["dirty"] = True
    _set_busy(state, True, "Reading your request…")
    prompt = build_final_prompt_from_chat(state["history"])
    background_tasks.create(_do_clarify(state, prompt), name=f"clarify prompt {state['run_id']}")
    _scroll_to_bottom(state)


async def _do_clarify(state: dict[str, Any], prompt: str) -> None:
    """Run the clarification step, record its understanding, open its questions.

    The understanding is ALWAYS recorded — even when the clarification found no
    ambiguity. It is part of the context the generation LLM receives (its own
    restatement of the intent and the questions it raised), and it is what lets
    the operator notice a change request read as a report BEFORE pressing Run.
    """
    instance = page_instance_url()
    credentials = credentials_for_instance(instance)
    if credentials is None:
        _set_busy(state, False)
        with state.get("_client"):
            ui.notify("Not logged in to Uwazi — log in again", type="negative")
        return
    try:
        understanding = await clarify_prompt(
            instance,
            prompt,
            credentials.user,
            credentials.password,
        )
    except Exception as exc:  # noqa: BLE001 — the chat reports; the page never crashes
        _set_busy(state, False)
        ui.notify(f"Clarification failed: {exc}", type="negative", multi_line=True)
        return
    _set_busy(state, False)
    summary = (understanding.summary or "").strip()
    if summary or understanding.questions:
        state["history"].append(
            ChatMessage(
                role=ChatRole.ASSISTANT,
                text=summary,
                stamp=_stamp(),
                status=ChatPromptStatus.APPLIED,
                questions=list(understanding.questions),
            )
        )
        _add_understanding_bubble(state, summary, understanding.questions)
    if understanding.questions:
        _open_questions_dialog(state, understanding.questions)


def _add_understanding_bubble(state: dict[str, Any], summary: str, questions: list[Any]) -> None:
    """Show what the clarification LLM understood, above its questions.

    Display-only context (no status badge): the read-back that catches a misread
    intent. Its text is also in the chat history, so the same words reach the
    generation LLM.
    """
    with state["_client"]:
        with state["_messages"]:
            with ui.column().classes("w-full gap-0"):
                with ui.row().classes("items-center gap-2 q-ml-sm no-wrap"):
                    ui.label("Understanding").classes("text-caption text-weight-medium")
                ui.chat_message(text=summary or "(no summary)", sent=False).classes(PROMPT_BUBBLE_CLASS)
                for question in questions:
                    ui.label(f"• {question.question}").classes("text-caption text-grey-7 q-ml-md")
    _scroll_to_bottom(state)


def _open_questions_dialog(state: dict[str, Any], questions: list[Any]) -> None:
    """Show the clarifications in their own modal.

    The questions use the same checkbox cards as the New task wizard's
    validation step: every suggested option is a checkbox plus a free-text
    field. The free-text answer counts on its own — no extra checkbox to forget.
    Sending the answers folds them into the request's existing bubble instead of
    appending an assistant message.
    """
    client = state.get("_client")
    with client.layout:
        previous = state.get("_questions_dialog")
        if previous is not None and not previous.is_deleted:
            previous.close()
            previous.delete()

        with ui.dialog() as dialog, ui.card().classes("w-full max-w-3xl uw-dialog").props("style=height:85dvh"):
            dialog.props("persistent")
            with ui.column().classes("w-full h-full items-stretch no-wrap"):
                modal_header(dialog, "Clarifying questions")
                with ui.scroll_area().classes("w-full grow q-mt-sm"):
                    with ui.column().classes("w-full gap-2"):
                        answer_widgets: list[dict[str, Any]] = []
                        for question in questions:
                            with ui.card().classes("w-full q-mb-sm uw-subcard"):
                                ui.label(question.question).classes("text-body1 text-weight-medium")
                                option_checkboxes = [ui.checkbox(option) for option in question.options]
                                custom_input = ui.input("Your answer").props("outlined dense").classes("w-full")
                                answer_widgets.append(
                                    {
                                        "question": question.question,
                                        "options": list(zip(question.options, option_checkboxes)),
                                        "custom_input": custom_input,
                                    }
                                )
                with ui.row().classes("uw-dialog-footer no-wrap shrink-0"):
                    ui.button("Skip", on_click=lambda: _skip_questions(state, dialog)).props("flat no-caps").classes(
                        "uw-btn-ghost"
                    )
                    ui.button(
                        "Send answers",
                        icon="send",
                        on_click=lambda: _submit_answers(state, dialog, answer_widgets),
                    ).props("unelevated no-caps color=primary")
        state["_questions_dialog"] = dialog
    dialog.open()


def _skip_questions(state: dict[str, Any], dialog: Any) -> None:
    """Close the clarifications without folding anything into the request."""
    dialog.close()
    state["_pending_index"] = None
    state["_pending_request"] = None
    _refresh_run_button(state)


def _submit_answers(state: dict[str, Any], dialog: Any, answer_widgets: list[dict[str, Any]]) -> None:
    """Fold the answers into the request's bubble (no new assistant turn).

    A free-text answer counts on its own: it is the operator's custom
    resolution, whether or not a suggested option was ticked.
    """
    answers = [
        QuestionAnswer(
            question=widget["question"],
            selected=[option for option, checkbox in widget["options"] if checkbox.value],
            custom=(widget["custom_input"].value or "").strip() or None,
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
    state["history"][index] = ChatMessage(
        role=ChatRole.OPERATOR,
        text=text,
        stamp=previous.stamp,
        status=previous.status,
    )
    previous_handle = state.get("_pending_bubble")
    _delete_bubble(state, previous_handle)
    handle = _add_prompt_bubble(state, text, previous.stamp, status=previous.status)
    state["_pending_bubble"] = handle
    # Keep the Run button's pending set pointing at the NEW bubble, so a
    # successful Run flips the one that is actually on screen.
    unrun = state["_unrun_bubbles"]
    for position, item in enumerate(unrun):
        if item is previous_handle:
            unrun[position] = handle
            break
    else:
        unrun.append(handle)
    _scroll_to_bottom(state)


def _mark_prompts_run(state: dict[str, Any]) -> None:
    """Mark every prompt written in this session as folded into the Run.

    Called only after the regeneration SUCCEEDED: the requests are now part of
    the script's prompt, so their bubbles say "Applied" instead of "Not applied yet".
    A failed Run leaves them pending (the operator can try again).
    """
    state["history"] = mark_prompts_applied(state["history"])
    for handle in state.get("_unrun_bubbles", []):
        _set_bubble_status(state, handle, ChatPromptStatus.APPLIED)
    state["_unrun_bubbles"] = []
    # No clarification answer may land on an already-applied prompt afterwards.
    state["_pending_index"] = None
    state["_pending_request"] = None
    state["_pending_bubble"] = None


# --- compaction ---------------------------------------------------------------


def _compact_clicked(state: dict[str, Any]) -> None:
    """The Compact button: rewrite the whole chat as ONE concise prompt.

    The rewrite is an LLM call over the WHOLE chat (the original prompt, the
    resolved clarifications and every request), so it runs in the background
    with the modal's own busy state. The table is NOT touched: the compaction
    only rewrites the CHAT, and the operator still presses Run to regenerate
    the script from it.
    """
    if state["busy"]:
        return
    if not state["history"]:
        ui.notify("There is nothing to compact yet", type="warning")
        return
    _set_busy(state, True, "Compacting the chat…")
    background_tasks.create(
        _do_compact(state),
        name=f"compact chat {state['run_id']}",
    )


async def _do_compact(state: dict[str, Any]) -> None:
    """Run the compaction and replace the chat with the rewrite.

    On failure NOTHING changes: the chat keeps its bubbles and its history, so
    the operator can retry.
    """
    try:
        compacted = await compact_chat(build_compaction_input(state["history"]))
    except Exception as exc:  # noqa: BLE001 — the chat reports; the modal stays open
        _set_busy(state, False)
        with state.get("_client"):
            ui.notify(f"Compaction failed: {exc}", type="negative", multi_line=True)
        return
    _replace_chat_with_compaction(state, compacted)
    _set_busy(state, False)
    with state.get("_client"):
        ui.notify("Chat compacted — press Apply modifications to apply it", type="positive", multi_line=True)


def _replace_chat_with_compaction(state: dict[str, Any], compacted: str) -> None:
    """Swap the chat's bubbles and history for the compacted prompt.

    The run's own context survives as the base bubble (the original prompt and
    its resolved clarifications): the run itself did not change, only the
    conversation did. Every other bubble is replaced by the ONE compacted
    prompt, which keeps the run-status badge of the conversation it summarizes
    (applied when every request was already Run).
    """
    messages = state["_messages"]
    stamp = _stamp()
    history = apply_compaction(state["history"], compacted, stamp)
    state["history"] = history
    # The compacted prompt is the only prompt left, so the pending set is
    # exactly the new bubble (when it still has to be Run).
    state["_unrun_bubbles"] = []
    state["_pending_index"] = None
    state["_pending_request"] = None
    state["_pending_bubble"] = None
    state["dirty"] = any(
        message.role is ChatRole.OPERATOR and message.status is ChatPromptStatus.PENDING for message in history
    )

    with state.get("_client"):
        messages.clear()
        with messages:
            for label, text, message_stamp, status in _compacted_bubbles(history):
                handle = _add_prompt_bubble(state, text, message_stamp, label=label, status=status)
                if status is ChatPromptStatus.PENDING:
                    state["_unrun_bubbles"].append(handle)
    _scroll_to_bottom(state)
    _refresh_run_button(state)


def _compacted_bubbles(history: list[ChatMessage]) -> list[tuple[str, str, str, ChatPromptStatus]]:
    """The bubbles a compacted chat renders (pure).

    The base prompt (the original + its resolved clarifications) first, then
    the ONE compacted prompt — the whole conversation collapsed into it.
    """
    bubbles: list[tuple[str, str, str, ChatPromptStatus]] = []
    system = [message for message in history if message.role is ChatRole.SYSTEM]
    if system:
        baseline = build_final_prompt_from_chat(system).strip()
        if baseline:
            bubbles.append(("Base prompt", baseline, "", ChatPromptStatus.APPLIED))
    compacted = [message for message in history if message.role is ChatRole.OPERATOR]
    for message in compacted:
        bubbles.append(("Compacted prompt", message.text, message.stamp, message.status))
    return bubbles


def _set_busy(state: dict[str, Any], busy: bool, message: str = "") -> None:
    """Flip the busy state, refresh the actions, and show/hide the progress row."""
    state["busy"] = busy
    _set_send_enabled(state, not busy)
    _set_compact_enabled(state, not busy)
    _set_run_progress(state, message if busy else "", visible=busy)
    _refresh_run_button(state)


def _set_run_progress(state: dict[str, Any], message: str, *, visible: bool) -> None:
    """Show the in-modal spinner + the backend's current stage."""
    spinner = state.get("_run_spinner")
    label = state.get("_run_status")
    with state.get("_client"):
        if spinner is not None and not spinner.is_deleted:
            spinner.set_visibility(visible)
        if label is not None and not label.is_deleted:
            label.set_text(message)


def _set_send_enabled(state: dict[str, Any], enabled: bool) -> None:
    """Enable or disable the one input action."""
    button = state.get("_send_button")
    if button is not None and not button.is_deleted:
        with state.get("_client"):
            button.enable() if enabled else button.disable()


def _set_compact_enabled(state: dict[str, Any], enabled: bool) -> None:
    """Enable or disable the Compact button (disabled while a turn runs)."""
    button = state.get("_compact_button")
    if button is not None and not button.is_deleted:
        with state.get("_client"):
            button.enable() if enabled else button.disable()


def _refresh_run_button(state: dict[str, Any]) -> None:
    """Show the Apply button only when the chat has changes and nothing is running."""
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


def _run_clicked(
    state: dict[str, Any],
    on_table_changed: Callable[[], None] | None,
    on_run_started: Callable[[], None] | None = None,
) -> None:
    """The Apply button: regenerate the script from the whole chat and reconcile."""
    if state["busy"] or not state["dirty"]:
        return
    budget = build_budget(_regeneration_estimate(state["run_id"]))
    _start_run(state, on_table_changed, budget, on_run_started)


def _start_run(
    state: dict[str, Any],
    on_table_changed: Callable[[], None] | None,
    budget: LlmBudget,
    on_run_started: Callable[[], None] | None = None,
) -> None:
    """Regenerate in the background (no spend confirmation), with visible progress.

    The run is CLAIMED before the background task starts, with the same
    per-run claim every other local operation takes. That is what makes the
    tasks table show ``regenerating`` and refuses a discard/restore/Run on the
    same run while the planning pass rewrites its table; other runs and other
    instances stay untouched.
    """
    instance = page_instance_url()
    run_id = state["run_id"]
    if not try_claim_run(instance, run_id, "regenerating"):
        _notify_instance(instance, run_claim_text(run_claim(instance, run_id)), type_="warning", multi_line=True)
        return
    # Push the claim into every tasks table BEFORE the optional navigation, so
    # a client that is already showing the row repaints it, and the page we
    # navigate to reads the claim when it builds.
    _broadcast_run_rows(instance)
    _set_busy(state, True, "Regenerating the script from the chat…")
    _scroll_to_bottom(state)
    # A toast covers the case where the operator is scrolled away from the
    # in-modal indicator; it auto-hides, and the final result gets its own toast.
    _notify_instance(
        instance,
        "Regenerating the script and recomputing the table…",
        type_="ongoing",
        timeout=8000,
    )
    background_tasks.create(
        _do_run(state, on_table_changed, budget, instance),
        name=f"chat regeneration {run_id}",
    )
    if on_run_started is not None:
        on_run_started()


async def _do_run(
    state: dict[str, Any],
    on_table_changed: Callable[[], None] | None,
    budget: LlmBudget,
    instance: str,
) -> None:
    """Regenerate the script from the chat and reconcile the live table.

    The dialog may be gone by the time this finishes (the review page navigates
    to the tasks window when the Run starts), so every notification goes to the
    instance's live clients and the page refresh is skipped for a deleted one.
    The claim is ALWAYS released, whatever the outcome.
    """
    run_id = state["run_id"]
    client = state.get("_client")

    def _on_stage(message: str) -> None:
        # Two audiences: the modal's own spinner line, and the tasks table's
        # status caption (read from the run's claim), so the work is visible
        # even after the review page navigated to the tasks window.
        _set_run_progress(state, message, visible=True)
        set_claim_note(instance, run_id, message)

    try:
        credentials = credentials_for_instance(instance)
        if credentials is None:
            _set_busy(state, False)
            _notify_instance(instance, "Not logged in to Uwazi — log in again", type_="negative")
            return
        try:
            result = await regenerate_from_chat(
                instance,
                run_id,
                state["history"],
                credentials.user,
                credentials.password,
                budget,
                on_stage=_on_stage,
            )
        except Exception as exc:  # noqa: BLE001 — the chat reports; the modal stays open
            _set_busy(state, False)
            _notify_instance(instance, f"Regeneration failed: {exc}", type_="negative", multi_line=True)
            return
        state["dirty"] = False
        _mark_prompts_run(state)
        _set_busy(state, False)
        _notify_instance(instance, result.message(), type_="positive", multi_line=True)
        if on_table_changed is not None and client is not None and not client.is_deleted:
            on_table_changed()
    finally:
        release_run(instance, run_id, "regenerating")
        _broadcast_run_rows(instance)


__all__ = ["prompt_chat_dialog"]
