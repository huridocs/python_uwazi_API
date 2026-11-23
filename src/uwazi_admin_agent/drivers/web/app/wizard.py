"""Web driver module split out of the original ``app`` (behavior preserved)."""

from __future__ import annotations

import asyncio
from typing import Any

from nicegui import background_tasks, ui

from uwazi_admin_agent.configuration import (
    LLM_PRICE_INPUT_PER_MTOK,
    LLM_PRICE_OUTPUT_PER_MTOK,
    PREDICTION_DEFAULT_MAX_COST,
    PREDICTION_DEFAULT_MAX_REQUESTS,
    SCRIPT_DEFAULT_MAX_COST,
)
from uwazi_admin_agent.domain.guided_task import GOAL_CUSTOM, GOAL_DESCRIPTIONS, GOAL_FILL, GOALS
from uwazi_admin_agent.domain.llm_budget import LlmBudget, LlmPrice, with_default_backstops
from uwazi_admin_agent.domain.prompt_validation import QuestionAnswer, build_final_prompt
from uwazi_admin_agent.drivers.web import operation_gate

# Import for its side effect: the ``@ui.page("/review/{run_id}")`` route registers
# on import, so the runs table's Review action can navigate to it.
from uwazi_admin_agent.drivers.web import review_page as _review_page_module  # noqa: F401
from uwazi_admin_agent.drivers.web import task_page as _task_page_module  # noqa: F401
from uwazi_admin_agent.drivers.web.run_service import (
    clarify_prompt,
    create_and_generate,
    list_runs,
)
from uwazi_admin_agent.drivers.web.session import (
    credentials_for_instance,
    page_instance_url,
)
from uwazi_admin_agent.drivers.web.task_dialogs import (
    modal_header,
)

from .broadcast import _broadcast_rows
from .guided_form import build_guided_step, ensure_guided_form, start_guided_flow
from .state import _broadcast_notify, _busy_run_text, _notify_error, _session_ended

_PROMPT_SOURCE_GUIDED = "guided"


def _new_task_wizard(prefill_name: str = "", prefill_prompt: str = "", prefill_validated_prompt: str | None = None) -> None:
    """Open the task-type-first wizard, or Generate directly for a duplicate."""
    base_prompt = (prefill_validated_prompt or prefill_prompt).strip() or prefill_prompt
    state: dict[str, Any] = {"name": prefill_name, "prompt": prefill_prompt, "final_prompt": base_prompt}
    state["_clarify_base"] = base_prompt
    if prefill_name or prefill_prompt:
        state["_prompt_base"] = base_prompt
        state["validated_prompt"] = base_prompt

    with ui.dialog() as dialog, ui.card().classes("w-full max-w-4xl"):
        dialog.props("persistent")
        modal_header(dialog, "New task")
        with ui.stepper().classes("w-full") as stepper:
            # Guided questions have their own progress indicator; outer tabs
            # would expose irrelevant branches and allow answers to be skipped.
            stepper.props("no-header")
            state["_dialog"] = dialog
            state["_stepper"] = stepper
            _wizard_step_task(state, stepper, dialog, prefill_name=prefill_name, prefill_prompt=prefill_prompt)
            _wizard_step_guided(state, dialog)
            _wizard_step_validate(state, stepper, dialog)
            _wizard_step_caps()
            _wizard_step_generate(state, dialog, stepper)
        dialog.open()
        if prefill_name or prefill_prompt:
            stepper.set_value("generate")
            _show_generate_prompt(state)


def _wizard_step_task(
    state: dict[str, Any],
    stepper: Any,
    dialog: Any,
    prefill_name: str = "",
    prefill_prompt: str = "",
) -> None:
    """Build the opening run-name screen, task-type choice, and custom prompt."""
    with ui.step(name="name", title="Name", icon="edit"):
        ui.label("Name the run").classes("text-h6 q-mb-md")
        name_input = ui.input(
            "Run name",
            placeholder="e.g. classify-judgments-2026",
            value=prefill_name,
            validation={"Required": lambda v: bool(v and v.strip())},
        ).classes("w-full text-h6")
        state["_details_name_input"] = name_input
        with ui.row().classes("q-mt-lg w-full justify-end"):
            ui.button("Cancel", on_click=lambda: dialog.close()).props("color=grey-7 flat")
            ui.button("Next", icon="arrow_forward", on_click=lambda: _wizard_name_next(state)).props("color=primary")

    with ui.step(name="goal", title="Task type", icon="category"):
        ui.label("What would you like to do?").classes("text-h6 q-mb-md")
        goal_input = ui.radio(
            {goal: GOAL_DESCRIPTIONS[goal] for goal in GOALS},
            value=GOAL_FILL,
        ).classes("wizard-options wizard-goal-options w-full")
        state["_goal_input"] = goal_input
        with ui.row().classes("q-mt-lg w-full justify-between"):
            ui.button("Back", on_click=lambda: stepper.set_value("name")).props("color=grey-7 flat")
            ui.button("Next", icon="arrow_forward", on_click=lambda: _wizard_goal_next(state)).props("color=primary")

    with ui.step(name="custom", title="Custom task", icon="edit_note"):
        ui.label("Describe the custom analysis or data-processing rule.").classes("text-h6 q-mb-md")
        prompt_input = (
            ui.textarea(
                "Task description",
                placeholder="Explain what to inspect, what logic to apply, and what suggestions to record.",
                value=prefill_prompt,
                validation={"Required": lambda v: bool(v and v.strip())},
            )
            .classes("w-full")
            .props("outlined autogrow input-style='min-height: 220px;'")
        )
        state["_details_prompt_input"] = prompt_input
        with ui.row().classes("q-mt-lg w-full justify-between"):
            ui.button("Back", on_click=lambda: stepper.set_value("goal")).props("color=grey-7 flat")
            ui.button("Next", icon="arrow_forward", on_click=lambda: _custom_prompt_next(state)).props("color=primary")


def _wizard_name_next(state: dict[str, Any]) -> None:
    """Validate the run name, then ask what the task should do."""
    if _valid_run_name(state, state["_details_name_input"]) is None:
        return
    state["_stepper"].set_value("goal")


def _valid_run_name(state: dict[str, Any], name_input: Any) -> str | None:
    """Validate the run name (and its availability); ``None`` when it is refused."""
    name = (name_input.value or "").strip()
    if not name:
        ui.notify("Name is required", type="warning")
        return None
    instance = page_instance_url()
    if _session_ended(instance):
        return None
    if name in {r.run_id for r in list_runs(instance)}:
        ui.notify(f"A run named {name!r} already exists", type="warning")
        return None
    state["name"] = name
    return name


def _wizard_goal_next(state: dict[str, Any]) -> None:
    """Route the selected task type to guided questions or custom entry."""
    goal = state["_goal_input"].value
    if goal == GOAL_CUSTOM:
        state["_stepper"].set_value("custom")
        return
    state["_prompt_source"] = _PROMPT_SOURCE_GUIDED
    state["_stepper"].set_value("guided")
    start_guided_flow(state, goal)


def _custom_prompt_next(state: dict[str, Any]) -> None:
    prompt = (state["_details_prompt_input"].value or "").strip()
    if not prompt:
        ui.notify("Task description is required", type="warning")
        return
    state["_prompt_source"] = GOAL_CUSTOM
    _advance_with_prompt(state, prompt, state["_stepper"])


def _advance_with_prompt(state: dict[str, Any], prompt: str, stepper: Any) -> None:
    """Store ``prompt`` and advance to validate, dropping stale clarifications.

    A duplicate opens with the source run's FOLDED prompt (original +
    clarifications) already applied. Stepping back to this panel must not
    silently drop it: only an EDITED request invalidates those clarifications,
    because they answered the previous wording. A guided prompt is always a new
    request.
    """
    unchanged = prompt == state.get("prompt")
    if not unchanged:
        state["validated_prompt"] = None
        state["_prompt_base"] = prompt
        state["_clarify_base"] = prompt
    state["prompt"] = prompt
    state["final_prompt"] = state.get("validated_prompt") or prompt
    state["_validation_done"] = False
    _render_validate_idle(state, state["_validate_container"])
    stepper.set_value("validate")


def _guided_prompt_ready(state: dict[str, Any], prompt: str) -> None:
    """Accept the assembled prompt and continue to validation."""
    state["_prompt_source"] = _PROMPT_SOURCE_GUIDED
    _advance_with_prompt(state, prompt, state["_stepper"])


def _wizard_back_to_prompt_step(state: dict[str, Any]) -> None:
    """Back from validate/generate to the screen that produced the prompt."""
    source = state.get("_prompt_source")
    destination = "guided" if source == _PROMPT_SOURCE_GUIDED else "custom" if source == GOAL_CUSTOM else "goal"
    state["_stepper"].set_value(destination)
    if destination == "guided":
        ensure_guided_form(state)


def _guided_back_to_goal(state: dict[str, Any]) -> None:
    state["_stepper"].set_value("goal")


def _guided_switch_to_free_text(state: dict[str, Any]) -> None:
    """Fall back to custom entry when the instance schema cannot be read."""
    state["_goal_input"].value = GOAL_CUSTOM
    state["_prompt_source"] = GOAL_CUSTOM
    state["_stepper"].set_value("custom")


def _wizard_step_guided(state: dict[str, Any], dialog: Any) -> None:
    build_guided_step(
        state,
        dialog,
        on_submit=lambda prompt, _answers: _guided_prompt_ready(state, prompt),
        on_back=lambda: _guided_back_to_goal(state),
        on_free_text=lambda: _guided_switch_to_free_text(state),
    )


def _wizard_step_validate(state: dict[str, Any], stepper: Any, dialog: Any) -> None:
    with ui.step(name="validate", title="Validate", icon="help"):
        container = ui.column().classes("w-full")
        state["_validate_container"] = container
        _render_validate_idle(state, container)


def _render_validate_idle(state: dict[str, Any], container: Any) -> None:
    """Show the pre-validation state: a Validate button (validation is opt-in)."""
    container.clear()
    with container:
        ui.label("Ask the LLM what it understood from your prompt before generating.").classes("text-body1 q-mb-md")
        with ui.row().classes("q-mt-lg w-full justify-end"):
            ui.button("Cancel", on_click=lambda: state["_dialog"].close()).props("color=grey-7 flat")
            ui.button("Back", on_click=lambda: _wizard_back_to_prompt_step(state)).props("color=grey-7 flat")
            ui.button("Skip", on_click=lambda: _wizard_skip(state)).props("color=grey-7 flat")
            ui.button("Validate", icon="help", on_click=lambda: _start_validation(state)).props("color=primary")


def _start_validation(state: dict[str, Any]) -> None:
    """Kick off the prompt-validation LLM call and show a loading state."""
    if not (state.get("prompt") or "").strip():
        # The stepper header can jump straight here: validating nothing would
        # ask the LLM to interpret an empty request.
        ui.notify("Write or build a prompt first", type="warning")
        _wizard_back_to_prompt_step(state)
        return
    container = state["_validate_container"]
    container.clear()
    with container:
        with ui.row().classes("w-full items-center gap-2"):
            ui.spinner(size="lg")
            ui.label("Validating prompt — asking the LLM what it understood...").classes("text-body1")
    background_tasks.create(_do_clarify(state, container), name="clarify prompt")


async def _do_clarify(state: dict[str, Any], container: Any) -> None:
    """Run the clarification LLM call on a worker thread, then render the result."""
    # Validate the prompt that would actually be SENT, not the bare original: a
    # duplicate (or a run whose validation already resolved questions) carries
    # its clarifications in ``_clarify_base``, and re-validating the naked
    # request would ask the LLM to answer questions the operator already settled.
    prompt = state.get("_clarify_base") or state["prompt"]
    instance = page_instance_url()
    credentials = credentials_for_instance(instance)
    if credentials is None:
        _render_validation_error(state, container, RuntimeError("your session ended — log in again"))
        return
    try:
        understanding = await asyncio.to_thread(
            asyncio.run,
            clarify_prompt(
                instance,
                prompt,
                credentials.user,
                credentials.password,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        _render_validation_error(state, container, exc)
        return
    state["understanding"] = understanding
    _render_validation(state, container)


def _render_validation_error(state: dict[str, Any], container: Any, exc: Exception) -> None:
    """Show a validation failure with a skip affordance (the operator can proceed)."""
    state["_validation_done"] = True
    container.clear()
    with container:
        ui.label("Validation failed").classes("text-h6 text-negative")
        ui.label(str(exc)).classes("text-body2 text-red-10 q-mb-md")
        with ui.row().classes("q-mt-lg w-full justify-end"):
            ui.button("Cancel", on_click=lambda: state["_dialog"].close()).props("color=grey-7 flat")
            ui.button("Back", on_click=lambda: _wizard_back_to_prompt_step(state)).props("color=grey-7 flat")
            ui.button("Skip", on_click=lambda: _wizard_skip(state)).props("color=primary")


def _render_validation(state: dict[str, Any], container: Any) -> None:
    """Render the LLM's understanding: summary + clarifying questions + notes + preview."""
    state["_validation_done"] = True
    understanding = state["understanding"]
    container.clear()
    with container:
        ui.label("What the LLM understood").classes("text-subtitle1 text-grey-7")
        ui.label(understanding.summary).classes("text-body1 q-mb-md")

        answer_widgets: list[dict[str, Any]] = []
        if understanding.questions:
            ui.label("Clarifying questions").classes("text-subtitle1 text-grey-7 q-mt-md")
            for question in understanding.questions:
                with ui.card().classes("w-full q-mb-sm"):
                    ui.label(question.question).classes("text-body1 text-weight-medium")
                    option_checkboxes = [
                        ui.checkbox(option, on_change=lambda: _refresh_final_prompt(state)) for option in question.options
                    ]
                    # The free-text field IS the custom answer: whatever the
                    # operator types there is folded into the prompt, with no
                    # extra checkbox to forget. Gating it on a checkbox is how
                    # a typed correction silently vanished.
                    custom_input = ui.input("Your answer", on_change=lambda: _refresh_final_prompt(state)).classes("w-full")
                    answer_widgets.append(
                        {
                            "question": question.question,
                            "options": list(zip(question.options, option_checkboxes)),
                            "custom_input": custom_input,
                        }
                    )
        state["_answer_widgets"] = answer_widgets

        ui.label("Additional notes").classes("text-subtitle1 text-grey-7 q-mt-md")
        notes_input = (
            ui.textarea("Notes", on_change=lambda: _refresh_final_prompt(state)).classes("w-full").props("autogrow")
        )
        state["_notes_input"] = notes_input

        ui.label("Final prompt (sent to the LLM)").classes("text-subtitle1 text-grey-7 q-mt-md")
        preview = ui.textarea().classes("w-full font-mono").props("readonly outlined autogrow").style("min-height: 120px")
        state["_preview"] = preview

        with ui.row().classes("q-mt-lg w-full justify-end"):
            ui.button("Cancel", on_click=lambda: state["_dialog"].close()).props("color=grey-7 flat")
            ui.button("Back", on_click=lambda: _wizard_back_to_prompt_step(state)).props("color=grey-7 flat")
            ui.button("Skip", on_click=lambda: _wizard_skip(state)).props("color=grey-7 flat")
            ui.button("Confirm", on_click=lambda: _wizard_confirm(state)).props("color=primary")

        _refresh_final_prompt(state)


def _collect_answers(state: dict[str, Any]) -> list[QuestionAnswer]:
    """Read the current checkbox/input values into :class:`QuestionAnswer` objects.

    The free-text field counts on its own: a non-empty answer is the operator's
    custom resolution whether or not any suggested option was ticked.
    """
    answers: list[QuestionAnswer] = []
    for widget in state.get("_answer_widgets", []):
        selected = [option for option, checkbox in widget["options"] if checkbox.value]
        custom = (widget["custom_input"].value or "").strip() or None
        answers.append(QuestionAnswer(question=widget["question"], selected=selected, custom=custom))
    return answers


def _refresh_final_prompt(state: dict[str, Any]) -> None:
    """Recompute the final prompt from the current answers + notes and update the preview.

    Folds onto the SAME text the clarification pass was shown (``_clarify_base``),
    not the bare original: a duplicate's questions were asked about the source
    run's folded prompt, so folding the answers into the naked request would
    drop the clarifications they were asked about. A fresh wizard's base is the
    operator's request itself, where both coincide.
    """
    notes_input = state.get("_notes_input")
    notes = (notes_input.value or "") if notes_input is not None else ""
    base = state.get("_clarify_base") or state["prompt"]
    final = build_final_prompt(base, _collect_answers(state), notes)
    state["final_prompt"] = final
    preview = state.get("_preview")
    if preview is not None:
        preview.value = final


def _wizard_skip(state: dict[str, Any]) -> None:
    """Proceed without running a NEW validation pass.

    The base is the prompt already resolved for this wizard: a duplicate opens
    with the source run's clarifications applied, and "Skip" here means "do not
    ask the LLM again", never "throw those clarifications away".
    """
    base = state.get("validated_prompt") or state["prompt"]
    if not (base or "").strip():
        # Reached by a header jump before any prompt exists: generate would
        # have nothing to send.
        ui.notify("Write or build a prompt first", type="warning")
        _wizard_back_to_prompt_step(state)
        return
    state["final_prompt"] = base
    state["_prompt_base"] = base
    _show_generate_prompt(state)
    state["_stepper"].set_value("generate")


def _wizard_confirm(state: dict[str, Any]) -> None:
    """Proceed to generate with the validated (final) prompt."""
    _refresh_final_prompt(state)
    state["validated_prompt"] = state["final_prompt"]
    state["_prompt_base"] = state["validated_prompt"]
    _show_generate_prompt(state)
    state["_stepper"].set_value("generate")


def _show_generate_prompt(state: dict[str, Any]) -> None:
    """Update the generate step's prompt display to the current final prompt."""
    _refresh_generate_prompt(state)


def _refresh_generate_prompt(state: dict[str, Any]) -> None:
    """Recompute the prompt the Generate button will send, including last-minute notes.

    This step is the LAST surface before generation, and the prompt it shows is
    what the operator is approving. Typing here used to be impossible: the
    textarea was read-only and only the validation step's notes reached the
    prompt, so a correction made at this point ("only 5 entities") silently
    vanished. The display now tracks the notes field, and ``_wizard_generate``
    sends exactly what the display shows.
    """
    notes_input = state.get("_generate_notes_input")
    notes = (notes_input.value or "").strip() if notes_input is not None else ""
    # A STABLE base, captured once: recomputing from ``final_prompt`` (which
    # already carries the notes) appends the same block on every keystroke.
    base = state.get("_prompt_base")
    if base is None:
        base = state.get("validated_prompt") or state.get("prompt") or ""
        state["_prompt_base"] = base
    state["final_prompt"] = f"{base}\n\nAdditional notes:\n{notes}" if notes else base
    display = state.get("_generate_prompt_display")
    if display is not None:
        display.value = state["final_prompt"]


def _wizard_generate_back(state: dict[str, Any]) -> None:
    """Back from the generate step: to caps, validate, or the prompt step."""
    if state.get("_validation_done"):
        target = "caps"
    else:
        _wizard_back_to_prompt_step(state)
        return
    state["_stepper"].set_value(target)


def _wizard_step_caps(state: dict[str, Any], stepper: Any, dialog: Any) -> None:
    """One step that explains and caps the AI allowance the finished task may use.

    Written for an operator who has never seen a script or an LLM call: it says
    what the finished thing is, when it might ask the AI, and the one money
    limit that bounds those asks. The request-count backstop is derived here —
    the operator only sets money.
    """
    with ui.step(name="caps", title="AI allowance", icon="payments"):
        ui.label("One last decision before the task is created: how much of the AI it may use.").classes(
            "text-body1 q-mb-md"
        )
        with ui.card().classes("w-full"):
            ui.label("What is about to happen?").classes("text-subtitle1")
            ui.label(
                "We turn your request into a small computer program, built once and then applied "
                "to every item you selected. That one-time build always uses the AI and is covered "
                "by the service's own standard limits — there is nothing to set for it here."
            ).classes("text-body2")
            ui.label("When would the task use the AI afterwards?").classes("text-subtitle1 q-mt-sm")
            ui.label(
                "For most requests the program works the results out by itself, at no cost. Only "
                "when your request asks for something a formula cannot decide — reading a document "
                "and writing a summary, for example — it will ask the AI about each of those items, "
                "which costs a small amount of money per item."
            ).classes("text-body2")
            ui.label("Your limit: the most money the task may spend on those asks").classes("text-subtitle1 q-mt-sm")
            ui.label(
                "The task stops as soon as this limit is reached; anything it already worked "
                "out is kept. At 0 the task never asks the AI — if it needed to, it would stop "
                "with an error asking you to come back and raise this limit."
            ).classes("text-body2")
            with ui.row().classes("w-full items-center gap-2 q-mt-sm"):
                state["_caps_cost_input"] = ui.number(
                    "Maximum spend ($)",
                    value=SCRIPT_DEFAULT_MAX_COST,
                    min=0.0,
                    step=0.5,
                    format="%.2f",
                ).classes("grow")
            ui.label("A typical summary-style task over a few hundred items costs well under a dollar.").classes(
                "text-body2 text-grey-7 q-mt-sm"
            )
        with ui.row().classes("q-mt-lg w-full justify-end"):
            ui.button("Cancel", on_click=lambda: dialog.close()).props("color=grey-7 flat")
            ui.button("Back", on_click=lambda: _wizard_caps_back(state)).props("color=grey-7 flat")
            ui.button(
                "Next",
                icon="arrow_forward",
                on_click=lambda: stepper.set_value("generate"),
            ).props("color=primary")


def _wizard_caps_back(state: dict[str, Any]) -> None:
    """Back from the caps step: to validate (if it ran) or to the prompt step."""
    if state.get("_validation_done"):
        state["_stepper"].set_value("validate")
    else:
        _wizard_back_to_prompt_step(state)


def _wizard_step_generate(state: dict[str, Any], dialog: Any, stepper: Any) -> None:
    with ui.step(name="generate", title="Generate", icon="auto_awesome"):
        ui.label("Review and generate the task.").classes("text-body1 q-mb-md")
        ui.label().bind_text_from(state, "name", backward=lambda v: f"Run: {v}").classes("text-h6")
        ui.label("Final prompt (sent to the LLM)").classes("text-subtitle1 text-grey-7 q-mt-md")
        prompt_display = (
            ui.textarea().classes("w-full font-mono").props("readonly outlined autogrow").style("min-height: 120px")
        )
        state["_generate_prompt_display"] = prompt_display
        ui.separator()
        ui.label("Last chance to change the prompt").classes("text-subtitle1 text-bold q-mt-sm")
        ui.label(
            "Anything you type here is appended to the final prompt above and reaches the generation "
            "LLM. Use it for a late correction (e.g. 'only the first 5 entities') without stepping back."
        ).classes("text-body2 text-grey-7")
        state["_generate_notes_input"] = (
            ui.textarea("Extra instruction (optional)", on_change=lambda: _refresh_generate_prompt(state))
            .classes("w-full")
            .props("autogrow")
        )
        with ui.row().classes("q-mt-lg w-full justify-end"):
            ui.button("Cancel", on_click=lambda: dialog.close()).props("color=grey-7 flat")
            ui.button("Back", on_click=lambda: _wizard_generate_back(state)).props("color=grey-7 flat")
            ui.button(
                "Generate",
                icon="auto_awesome",
                on_click=lambda: _wizard_generate(state, dialog),
            )


def _start_generation(
    name: str,
    prompt: str,
    user: str,
    password: str,
    instance: str,
    validated_prompt: str | None = None,
    claimed: bool = False,
    budget: LlmBudget | None = None,
) -> None:
    """Register the in-flight placeholder + notification and launch generation.

    Shared by the new-task wizard and the retry path on a ``generation_failed``
    run so both produce the identical toast/table/background-task flow.

    Generation takes only a per-run claim: it writes the run's folder and the
    review table but never Uwazi, so it must not block (or be blocked by) a
    write pass on another run or instance.

    ``claimed`` is set by the retry path, which already holds the claim (taken
    before its destructive ``delete_run``); re-claiming here would fail and
    abort generation after the run was already deleted.
    """
    if not claimed and not operation_gate.try_claim_run(instance, name, "creating"):
        ui.notify(_busy_run_text(instance, name), type="warning", multi_line=True)
        return
    _broadcast_rows(instance)
    notification = ui.notification(
        "Generating script, then the automatic LLM-free selection scan (this may take a minute)...",
        type="ongoing",
        spinner=True,
        timeout=None,
    )
    background_tasks.create(
        _do_generate(name, prompt, user, password, instance, validated_prompt, budget, notification),
        name=f"generate {name}",
    )


def _wizard_generate(state: dict[str, Any], dialog: Any) -> None:
    name = state.get("name", "")
    prompt = state.get("prompt", "")
    if not name or not prompt:
        ui.notify("Name and prompt are required", type="warning")
        return
    # Recompute first: the notes field may have changed since the last repaint
    # (a value set programmatically does not always fire ``on_change``).
    _refresh_generate_prompt(state)
    budget = _wizard_budget(state)
    final_prompt = state.get("final_prompt") or prompt
    dialog.close()
    _confirm_generation(name, prompt, final_prompt, budget)


def _script_caps_model() -> str:
    """The configured model name (falls back to a placeholder when unreadable)."""
    try:
        from uwazi_agent.configuration import MODEL

        return MODEL
    except Exception:  # noqa: BLE001 — a missing/renamed constant must not break the wizard
        return "(model not configured)"


def _wizard_budget(state: dict[str, Any]) -> LlmBudget:
    """The approved budget for the script's llm_complete/llm_map calls (caps step).

    The operator sets ONLY money. ``with_default_backstops`` derives the
    request-count backstop the ledger still enforces: both at 0 keeps the
    deny-by-default (the pass aborts on the script's first LLM call), any
    positive cost fills the backstop with its standard default. An
    empty/invalid cost input falls back to the configured default.
    """
    cost_widget = state.get("_caps_cost_input")
    cost = _cap_float(cost_widget.value) if cost_widget is not None else None
    max_cost, max_requests = with_default_backstops(
        SCRIPT_DEFAULT_MAX_COST if cost is None else cost,
        0,
        default_cost=PREDICTION_DEFAULT_MAX_COST,
        default_requests=PREDICTION_DEFAULT_MAX_REQUESTS,
    )
    return LlmBudget(
        max_cost=max_cost,
        max_requests=max_requests,
        price=LlmPrice(
            model=_script_caps_model(),
            input_per_mtok=LLM_PRICE_INPUT_PER_MTOK,
            output_per_mtok=LLM_PRICE_OUTPUT_PER_MTOK,
        ),
    )


def _cap_float(value: Any) -> float | None:
    """Coerce a caps input to a non-negative float (None/invalid → None = 'use the default')."""
    try:
        parsed = float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return parsed if parsed is not None and parsed >= 0 else None


def _confirm_generation(name: str, prompt: str, validated_prompt: str | None, budget: LlmBudget) -> None:
    """A plain go/no-go confirm, then launch generation.

    Generation itself is bounded by the built-in caps (``MAX_LLM_CALLS``
    requests via pydantic-ai's ``UsageLimits``, ``LLM_MAX_OUTPUT_TOKENS`` per
    call). ``budget`` — approved in the wizard's caps step — is the HARD ceiling
    on the generated script's own llm_complete/llm_map calls, enforced during
    the planning pass that populates the review table.
    """
    ui.notification(
        "Generating the script — the planning pass runs right after and fills the review table under the approved LLM caps.",
        type="info",
        timeout=6,
    )
    instance = page_instance_url()
    credentials = credentials_for_instance(instance)
    if credentials is None:
        ui.notify("Not logged in to Uwazi — log in again", type="negative")
        return
    _start_generation(
        name,
        prompt,
        credentials.user,
        credentials.password,
        instance=instance,
        validated_prompt=validated_prompt,
        budget=budget,
    )


async def _do_generate(
    name: str,
    prompt: str,
    user: str,
    password: str,
    instance: str,
    validated_prompt: str | None = None,
    budget: LlmBudget | None = None,
    notification: Any = None,
) -> None:
    # create_and_generate does synchronous Uwazi HTTP + LLM calls; run it on a
    # worker thread so the event loop stays free to serve the UI and websocket.
    try:
        await asyncio.to_thread(
            asyncio.run,
            create_and_generate(
                instance,
                name,
                prompt,
                user,
                password,
                validated_prompt,
                budget,
            ),
        )
        _broadcast_notify(
            instance,
            f"Run {name!r} generated — the planning pass filled the review table.",
            type="positive",
        )
    except Exception as exc:  # noqa: BLE001
        _notify_error(instance, "Generation failed", str(exc))
    finally:
        operation_gate.release_run(instance, name, "creating")
        if notification is not None:
            notification.dismiss()
        _broadcast_rows(instance)
