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
    WIZARD_DEFAULT_AI_ALLOWANCE,
)
from uwazi_admin_agent.domain.allowance_note import allowance_note_to_user_block
from uwazi_admin_agent.domain.guided_task import (
    GOAL_CUSTOM,
    GOAL_DESCRIPTIONS,
    GOAL_FILL,
    GOALS,
    OUTPUT_SPECIFIC,
)
from uwazi_admin_agent.domain.llm_budget import LlmBudget, LlmPrice, with_default_backstops
from uwazi_admin_agent.domain.prompt_validation import QuestionAnswer, build_final_prompt
from uwazi_admin_agent.domain.validation_note import validation_note_to_user_block
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


def _wizard_path(state: dict[str, Any]) -> list[tuple[str, str]]:
    """The steps the current answers will visit, in order (pure-ish).

    The Quasar header wraps eight branchy steps onto two ragged rows, so the
    wizard renders its own single-line indicator. Only the steps the chosen
    task actually walks are listed: the guided questions replace the custom
    prompt, and the test set exists only for a fill-a-property flow.
    """
    source = state.get("_prompt_source")
    steps: list[tuple[str, str]] = [("name", "Name"), ("goal", "Task type")]
    if source == GOAL_CUSTOM:
        steps.append(("custom", "Custom task"))
    else:
        steps.append(("guided", "Questions"))
    # The test set exists ONLY for a guided fill-a-property flow: there is no
    # measured property (and no label) to score otherwise.
    if _validation_goal(state) == GOAL_FILL:
        steps.append(("test_set", "Test set"))
    steps.extend((("caps", "AI allowance"), ("understand", "AI check"), ("generate", "Generate")))
    return steps


def _sync_step_indicator(state: dict[str, Any], active: str | None) -> None:
    """Point the single-line step indicator at the step the stepper shows."""
    label = state.get("_step_label")
    progress = state.get("_step_progress")
    if label is None or getattr(label, "is_deleted", False):
        return
    path = _wizard_path(state)
    names = [name for name, _title in path]
    titles = dict(path)
    current = active if active in titles else names[0]
    index = names.index(current) + 1
    label.text = f"Step {index} of {len(path)} · {titles[current]}"
    if progress is not None and not getattr(progress, "is_deleted", False):
        progress.value = index / len(path)


def _new_task_wizard(
    prefill_name: str = "",
    prefill_prompt: str = "",
    prefill_validated_prompt: str | None = None,
    prefill_budget: LlmBudget | None = None,
    prefill_validation: Any | None = None,
) -> None:
    """Open the task-creation wizard, or Generate directly for a duplicate."""
    base_prompt = (prefill_validated_prompt or prefill_prompt).strip() or prefill_prompt
    state: dict[str, Any] = {"name": prefill_name, "prompt": prefill_prompt, "final_prompt": base_prompt}
    state["_clarify_base"] = base_prompt
    # A duplicate carries the SOURCE task's settings. The validation override is
    # used until the operator edits the request (see ``_advance_with_prompt``);
    # the budget is written into the caps controls below so the same
    # ``_wizard_budget`` path serves both fresh and duplicated tasks.
    state["_validation_config_override"] = prefill_validation
    _seed_validation_state(state, prefill_validation)
    if prefill_name or prefill_prompt:
        state["_prompt_base"] = base_prompt
        state["validated_prompt"] = base_prompt

    with ui.dialog() as dialog, ui.card().classes("w-full max-w-4xl"):
        dialog.props("persistent")
        modal_header(dialog, "New task")
        # The Quasar header lays the branchy steps out over two ragged rows, so
        # the wizard shows one line instead: "Step n of N · <title>". It follows
        # the steps the chosen task actually walks. The stepper's own header is
        # hidden with CSS (.wizard-stepper in page.py): it lists branches the
        # task will not visit and allows answers to be skipped.
        with ui.column().classes("w-full gap-1"):
            state["_step_label"] = ui.label().classes("text-caption text-grey-7")
            state["_step_progress"] = ui.linear_progress(value=0, show_value=False).classes("w-full")
        with ui.stepper().classes("wizard-stepper w-full") as stepper:
            state["_dialog"] = dialog
            state["_stepper"] = stepper
            _wizard_step_task(state, stepper, dialog, prefill_name=prefill_name, prefill_prompt=prefill_prompt)
            _wizard_step_guided(state, dialog)
            # The test set is defined BEFORE the two AI steps: the allowance and
            # the prompt-understanding questions both depend on what the score
            # will measure, not the other way round.
            _wizard_step_validation(state, stepper, dialog)
            _wizard_step_caps(state, stepper, dialog)
            _wizard_step_validate(state, stepper, dialog)
            _wizard_step_generate(state, dialog, stepper)
            stepper.on_value_change(lambda event: _sync_step_indicator(state, str(event.value or "")))
        if prefill_budget is not None:
            _apply_budget_prefill(state, prefill_budget)
        _sync_test_set_step(state)
        _sync_step_indicator(state, stepper.value)
        dialog.open()
        if prefill_name or prefill_prompt:
            stepper.set_value("generate")
            _show_generate_prompt(state)


def _seed_validation_state(state: dict[str, Any], config: Any | None) -> None:
    """Put a source task's test-set settings into the wizard's own fields (pure-ish).

    A duplicate opens ON the Generate step, so the test-set widgets are never
    built for it at that point; without seeding the state they would come up
    blank when the operator presses Back, and ``_wizard_validation_config``
    reads the state keys (not the override) for every re-render. The override is
    still what a wizard that never visits the step submits, so editing the
    request drops it (see ``_advance_with_prompt``) and the fields below are what
    the rebuild shows.
    """
    if config is None:
        return
    mode = str(getattr(config, "mode", "") or "none")
    state["_validation_mode_value"] = mode
    state["_validation_property_name"] = str(getattr(config, "property_name", "") or "")
    state["_validation_size_value"] = getattr(config, "sample_size", None)
    state["_validation_filter_value"] = str(getattr(config, "selector_explanation", "") or "")
    # The source run's sample is already frozen: hand the duplicate the SAME
    # entities and the selector that chose them, so it neither re-asks the LLM
    # (whose second answer could pick others) nor silently changes the score's
    # population. Editing the request clears this (``_advance_with_prompt``).
    if mode == "custom":
        state["_validation_selector_code"] = str(getattr(config, "selector_code", "") or "")
        state["_validation_shared_ids"] = list(getattr(config, "shared_ids", ()) or ())


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
        state["_prompt_source"] = GOAL_CUSTOM
        _sync_step_indicator(state, "custom")
        state["_stepper"].set_value("custom")
        return
    state["_prompt_source"] = _PROMPT_SOURCE_GUIDED
    # Start the flow FIRST: it records the goal the step indicator reads, so a
    # fill task already lists its test-set step on arrival.
    start_guided_flow(state, goal)
    _sync_step_indicator(state, "guided")
    state["_stepper"].set_value("guided")


def _custom_prompt_next(state: dict[str, Any]) -> None:
    prompt = (state["_details_prompt_input"].value or "").strip()
    if not prompt:
        ui.notify("Task description is required", type="warning")
        return
    state["_prompt_source"] = GOAL_CUSTOM
    _advance_with_prompt(state, prompt, state["_stepper"])


def _advance_with_prompt(state: dict[str, Any], prompt: str, stepper: Any) -> None:
    """Store ``prompt`` and advance to the test-set step, dropping stale clarifications.

    A duplicate opens with the source run's FOLDED prompt (original +
    clarifications) already applied. Stepping back to this panel must not
    silently drop it: only an edited request invalidates those clarifications,
    because they answered the previous wording. A guided prompt is always a new
    request.
    """
    unchanged = prompt == state.get("prompt")
    if not unchanged:
        state["validated_prompt"] = None
        state["_prompt_base"] = prompt
        state["_clarify_base"] = prompt
        # A duplicate's test-set recipe belongs to the SOURCE request. Once the
        # operator edits the request, the copy must configure its own set: drop
        # both the inherited recipe and the sample entities frozen for it.
        state["_validation_config_override"] = None
        state["_validation_selector_code"] = ""
        state["_validation_shared_ids"] = []
    state["prompt"] = prompt
    state["final_prompt"] = state.get("validated_prompt") or prompt
    state["_validation_done"] = False
    _render_validate_idle(state, state["_validate_container"])
    # The test set comes FIRST, then the AI allowance, then the AI check: the
    # allowance and the clarification questions are tailored to the score the
    # operator chose to measure. Only a fill-a-property flow HAS a test set —
    # there is no label to score against when the task creates entities or runs
    # a custom rule, so those walk straight past the step. Rebuild the content
    # HERE (not only at build time) so it follows the goal just chosen.
    _render_test_set_step(state, visited=True)
    _sync_test_set_step(state)
    stepper.set_value("test_set" if _validation_goal(state) == GOAL_FILL else "caps")


def _sync_test_set_step(state: dict[str, Any]) -> None:
    """Show the test-set step only while the chosen flow can measure a property.

    Entity-creation and custom tasks have no label to score against. Hiding the
    step (not merely skipping it) keeps it out of the widget tree entirely, so
    no navigation path can land the operator on a picker that cannot sample.
    """
    step = state.get("_test_set_step")
    if step is not None and not step.is_deleted:
        step.set_visibility(_validation_goal(state) == GOAL_FILL)


def _guided_prompt_ready(state: dict[str, Any], prompt: str, answers: Any) -> None:
    """Accept the assembled prompt and continue to validation.

    The SUBMITTED answers are stored before advancing: the test-set step (and
    every later decision) keys off their goal, so a stale draft must never make
    a create flow look like a fill flow.
    """
    if answers is not None:
        state["_guided_answers"] = answers
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
    _sync_step_indicator(state, "custom")
    state["_stepper"].set_value("custom")


def _wizard_step_guided(state: dict[str, Any], dialog: Any) -> None:
    build_guided_step(
        state,
        dialog,
        on_submit=lambda prompt, answers: _guided_prompt_ready(state, prompt, answers),
        on_back=lambda: _guided_back_to_goal(state),
        on_free_text=lambda: _guided_switch_to_free_text(state),
    )


def _wizard_step_validate(state: dict[str, Any], stepper: Any, dialog: Any) -> None:
    # Named "AI check" (not "Validate") so it is not confused with the test-set
    # step it used to sit beside.
    with ui.step(name="understand", title="AI check", icon="help"):
        container = ui.column().classes("w-full")
        state["_validate_container"] = container
        _render_validate_idle(state, container)


def _render_validate_idle(state: dict[str, Any], container: Any) -> None:
    """Show the pre-validation state: a Validate button (validation is opt-in)."""
    container.clear()
    with container:
        keeps_set = _validation_goal(state) == GOAL_FILL
        ui.label(
            "Ask the LLM what it understood from your prompt before generating. It also sees the test set you "
            "chose, so its questions can be about what the score will measure."
            if keeps_set
            else "Ask the LLM what it understood from your prompt before generating."
        ).classes("text-body1 q-mb-md")
        with ui.row().classes("q-mt-lg w-full justify-end"):
            ui.button("Cancel", on_click=lambda: state["_dialog"].close()).props("color=grey-7 flat")
            ui.button("Back", on_click=lambda: _wizard_validate_back(state)).props("color=grey-7 flat")
            ui.button("Skip", on_click=lambda: _wizard_skip(state)).props("color=grey-7 flat")
            ui.button("Check with the AI", icon="help", on_click=lambda: _start_validation(state)).props("color=primary")


def _wizard_validate_back(state: dict[str, Any]) -> None:
    """Back from the AI check: to the allowance step that precedes it."""
    state["_stepper"].set_value("caps")


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
            ui.label("Asking the AI what it understood...").classes("text-body1")
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
        # The test-set and allowance steps run BEFORE this call, so the operator's
        # decisions are folded into the clarify prompt: the LLM tailors its
        # restatement and questions to the score that will be measured and to
        # what the task can afford.
        understanding = await asyncio.to_thread(
            asyncio.run,
            clarify_prompt(
                instance,
                prompt,
                credentials.user,
                credentials.password,
                allowance_note=allowance_note_to_user_block(_wizard_budget(state)),
                validation_note=validation_note_to_user_block(_wizard_validation_config(state)),
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
            ui.button("Back", on_click=lambda: _wizard_validate_back(state)).props("color=grey-7 flat")
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

        # Deliberately NO prompt preview here: the Generate step shows the
        # exact text that will be sent, so showing it twice made the operator
        # read the same thing on two consecutive screens.
        with ui.row().classes("q-mt-lg w-full justify-end"):
            ui.button("Cancel", on_click=lambda: state["_dialog"].close()).props("color=grey-7 flat")
            ui.button("Back", on_click=lambda: _wizard_validate_back(state)).props("color=grey-7 flat")
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
    """Recompute the final prompt from the current answers + notes.

    Folds onto the SAME text the clarification pass was shown (``_clarify_base``),
    not the bare original: a duplicate's questions were asked about the source
    run's folded prompt, so folding the answers into the naked request would
    drop the clarifications they were asked about. A fresh wizard's base is the
    operator's request itself, where both coincide.
    """
    notes_input = state.get("_notes_input")
    notes = (notes_input.value or "") if notes_input is not None else ""
    base = state.get("_clarify_base") or state["prompt"]
    state["final_prompt"] = build_final_prompt(base, _collect_answers(state), notes)


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
    _render_generate_settings(state)


def _refresh_generate_prompt(state: dict[str, Any]) -> None:
    """Sync the generate step's display with the prompt the Generate button sends.

    The prompt is FINAL at this point: it was assembled on the prompt step and
    (optionally) rewritten by validation. There is deliberately no extra input
    here, so the text the operator reviews is exactly the text that is sent.
    """
    state["final_prompt"] = state.get("validated_prompt") or state.get("_prompt_base") or state.get("prompt") or ""
    display = state.get("_generate_prompt_display")
    if display is not None:
        display.value = state["final_prompt"]


def _wizard_generate_back(state: dict[str, Any]) -> None:
    """Back from the generate step: to the AI check that precedes it."""
    state["_stepper"].set_value("understand")


def _validation_goal(state: dict[str, Any]) -> str:
    """The task type the test set applies to ('' when the wizard has no goal yet).

    A CUSTOM task is answered in free text and never runs the guided questions,
    so ``_guided_answers`` may still hold a stale fill goal from a previous
    pick; the prompt source wins there. It has no measured property, hence no
    test set either.

    A DUPLICATE carries its source run's recipe (``_validation_config_override``)
    and never walked the guided questions, so it has no goal of its own: the
    recipe is what says the task measures a property, and that keeps the test-set
    step in the wizard's path — pressing Back from the Generate step has to land
    on the settings the copy inherited, not skip past them.
    """
    source = state.get("_prompt_source")
    if source == GOAL_CUSTOM:
        return GOAL_CUSTOM
    if source is None and _goal_of(state) == GOAL_CUSTOM:
        return GOAL_CUSTOM
    goal = _goal_of(state)
    if goal:
        return goal
    override = state.get("_validation_config_override")
    if str(getattr(override, "mode", "") or "") not in ("", "none"):
        return GOAL_FILL
    return ""


def _goal_of(state: dict[str, Any]) -> str:
    """The guided flow's goal, or '' when no questions were answered (pure-ish)."""
    return str(getattr(state.get("_guided_answers"), "goal", "") or "")


def _wizard_step_validation(state: dict[str, Any], stepper: Any, dialog: Any) -> None:
    """The test-set step: pick whether to keep a fair test set, and of what.

    It runs BEFORE the AI allowance and the AI check so both can be tailored to
    what the score measures. The content is rebuilt on entry (see
    ``_render_test_set_step``): the goal is chosen LATER in the wizard, so the
    build-time render cannot know whether a test set applies at all.
    """
    with ui.step(name="test_set", title="Test set", icon="science") as step:
        state["_test_set_step"] = step
        container = ui.column().classes("w-full")
        state["_test_set_container"] = container
        _render_test_set_step(state)


def _render_test_set_step(state: dict[str, Any], *, visited: bool = False) -> None:
    """(Re)build the test-set step's content for the current goal.

    ONLY the fill-a-property flow offers the picker: a measured property is a
    fill-flow concept (the property being filled is what the score reads, and
    the guided questions carry its template). Entity-creation and custom tasks
    get a plain statement instead — no radio, no fields, no way to park the
    wizard on a picker that could never sample anything.

    ``visited`` says the operator ARRIVED here, so what the step now holds is
    their own choice and overrides a duplicated task's inherited recipe.
    """
    container = state.get("_test_set_container")
    if container is None or container.is_deleted:
        return
    dialog = state["_dialog"]
    stepper = state["_stepper"]
    container.clear()
    with container:
        # No content outside the fill flow: navigation skips the step entirely
        # (an entity-creation task has no label data to score against).
        if _validation_goal(state) != GOAL_FILL:
            return
        state["_validation_step_visited"] = bool(state.get("_validation_step_visited")) or visited
        ui.label("Keep a fair test set?").classes("text-h6 q-mb-sm")
        ui.label(
            "A test set is a few entities kept away from the AI. The finished task is scored on them, "
            "so you see how well it really works instead of how well it fits the examples the AI read. "
            "Those entities are not changed by the task unless you promote them later."
        ).classes("text-body2 text-grey-7 q-mb-md")
        # The mode lives in the STATE, not only in the widget: this render runs
        # again whenever the step is re-entered (Back from the allowance, a
        # duplicated task), and a radio rebuilt from a literal default silently
        # reset the operator's choice to "automatic" every time.
        state["_validation_mode"] = ui.radio(
            {
                "none": "No test set — just run the task",
                "automatic": "Pick a sample automatically (recommended)",
                "custom": "Describe how to choose the sample (the AI writes the selector when the task is generated)",
            },
            value=str(state.get("_validation_mode_value") or "automatic"),
            on_change=lambda event: _validation_mode_changed(state, event.value),
        ).classes("wizard-options w-full")
        with ui.column().classes("w-full") as fields:
            state["_validation_fields"] = fields
        with ui.row().classes("q-mt-lg w-full justify-between"):
            ui.button("Cancel", on_click=lambda: dialog.close()).props("color=grey-7 flat")
            ui.button("Back", on_click=lambda: _wizard_validation_back(state)).props("color=grey-7 flat")
            ui.button("Next", icon="arrow_forward", on_click=lambda: _wizard_validation_next(state, stepper)).props(
                "color=primary"
            )
        _render_validation_fields(state)


def _validation_mode_changed(state: dict[str, Any], value: Any) -> None:
    """Record the chosen mode, then show the fields it needs.

    Storing it in the state is what survives a re-render of the step (see
    ``_render_test_set_step``); the widget alone is recreated each time.
    """
    state["_validation_mode_value"] = str(value or "none")
    state["_validation_step_visited"] = True
    _render_validation_fields(state)


def _wizard_validation_back(state: dict[str, Any]) -> None:
    """Back from the test set: the screen that produced the prompt precedes it."""
    _wizard_back_to_prompt_step(state)


def _wizard_validation_next(state: dict[str, Any], stepper: Any) -> None:
    """Validate the test-set answers, then continue to the AI allowance."""
    config = _wizard_validation_config(state)
    if config is not None and config.mode != "none":
        if not config.property_name.strip():
            ui.notify("Choose which property the score should measure, or turn the test set off", type="warning")
            return
        if not config.template_names:
            ui.notify(
                "A test set is sampled from the templates this task targets: pick one in the questions step, "
                "or turn the test set off",
                type="warning",
            )
            return
        if config.mode == "custom" and not config.selector_explanation.strip():
            ui.notify(
                "Describe how the test entities should be chosen, or use the automatic sample",
                type="warning",
            )
            return
    stepper.set_value("caps")


def _validation_templates(answers: Any) -> list[str]:
    """The task's target templates (source templates as a create-flow fallback).

    A wizard that carries a duplicated task's recipe has no guided answers of its
    own: the source run's templates are the population the copy still samples
    from, so they answer for it.
    """
    templates = [str(name) for name in getattr(answers, "templates", ()) or () if str(name).strip()]
    if not templates:
        templates = [str(name) for name in getattr(answers, "source_templates", ()) or () if str(name).strip()]
    return templates


def _validation_property_options(state: dict[str, Any]) -> dict[str, str]:
    """The property names the score can measure, with their display labels."""
    schema = state.get("_guided_schema")
    answers = state.get("_guided_answers")
    templates = _validation_templates(answers)
    goal = getattr(answers, "goal", "")
    target = str(getattr(answers, "target_property", "") or "").strip()
    if goal == "fill" and target:
        return {target: _validation_property_label(state, target)}
    options: dict[str, str] = {}
    try:
        from uwazi_admin_agent.domain.guided_task import property_choices

        options.update(dict(property_choices(schema, templates)))
    except Exception:  # noqa: BLE001 — the schema is best-effort
        options = {}
    if target:
        options[target] = _validation_property_label(state, target)
    return options


def _render_validation_fields(state: dict[str, Any]) -> None:
    """Show the fields the selected mode needs (one property + sample size/search).

    Every widget is built INSIDE ``container``. An event handler's default slot is
    the page root, so creating them without that context appended a fresh picker
    to the page on every option change — the duplicated/triplicated inputs the
    operator saw — and ``container.clear()`` could never remove them.
    """
    container = state.get("_validation_fields")
    if container is None or container.is_deleted:
        return
    mode = str(state.get("_validation_mode_value") or getattr(state.get("_validation_mode"), "value", "none") or "none")
    container.clear()
    with container:
        if mode == "none":
            ui.label("No score will be shown for this run. You can add a test set later from the review page.").classes(
                "text-body2 text-grey-7"
            )
            return
        options = _validation_property_options(state)
        typed = str(state.get("_validation_property_name") or "").strip()
        if options:
            selected = typed if typed in options else next(iter(options))
            state["_validation_property_name"] = selected
            state["_validation_property"] = (
                ui.select(
                    options,
                    value=selected,
                    label="Property to measure",
                    on_change=lambda event: state.update(_validation_property_name=str(event.value or "")),
                )
                .props("dense outlined")
                .classes("w-full")
            )
            ui.label(
                "The score covers this one property. The task may still change others; this is what the number measures."
            ).classes("text-caption text-grey-7 q-mb-sm")
        else:
            # ONE picker, never both: with no readable property list the operator
            # types the name and the dropdown is not rendered at all.
            state["_validation_property"] = ui.input(
                "Property to measure (name)",
                value=typed,
                on_change=lambda event: state.update(_validation_property_name=str(event.value or "")),
            ).classes("w-full")
            ui.label(
                "The score needs one measured property. Type its name (the wizard could not read the instance's "
                "templates for this prompt), or turn the test set off."
            ).classes("text-body2 text-grey-7")
        state["_validation_size"] = ui.number(
            "Max test entities",
            value=state.get("_validation_size_value", 30),
            min=5,
            max=200,
            precision=0,
            on_change=lambda event: state.update(_validation_size_value=event.value),
        ).classes("w-48")
        ui.label("Upper limit: the sample or the search can return fewer entities than this.").classes(
            "text-caption text-grey-7"
        )
        if mode == "custom":
            ui.label("How should the test entities be chosen?").classes("text-subtitle2 q-mt-sm")
            ui.label(
                "Describe the filter in your own words — any property filter, a date window, a template, or "
                "explicit shared ids. When you generate the task, the AI writes the read-only selector that "
                "picks the sample, and the task is then built without reading those entities."
            ).classes("text-caption text-grey-7")
            state["_validation_filter"] = (
                ui.textarea(
                    "Test-set filter",
                    value=str(state.get("_validation_filter_value") or ""),
                    placeholder="e.g. judgments from the Kigali court dated 2024 whose summary is filled, or "
                    "these shared ids: abc123, def456",
                    on_change=lambda event: state.update(_validation_filter_value=str(event.value or "")),
                )
                .props("outlined autogrow")
                .classes("w-full")
            )
        if not _validation_templates(state.get("_guided_answers")) and not getattr(
            state.get("_validation_config_override"), "template_names", None
        ):
            # The sample is drawn per template; with no template there is no
            # population to sample from. Saying so is honest — silently creating
            # no set would look like the feature did nothing.
            ui.label(
                "A test set is sampled from the templates this task targets. Pick one in the questions step, "
                "or turn the test set off (a custom task can add one later from the review page)."
            ).classes("text-body2 text-warning")


def _validation_property_label(state: dict[str, Any], name: str) -> str:
    """``Label (name) — type`` for one property, from the wizard's schema (pure-ish)."""
    schema = state.get("_guided_schema")
    try:
        for template in getattr(schema, "templates", ()) or ():
            for prop in template.properties:
                if prop.name == name:
                    return prop.display
    except Exception:  # noqa: BLE001
        pass
    return name


def _wizard_validation_config(state: dict[str, Any]):
    """The wizard's validation answer as a :class:`ValidationConfig` (or None)."""
    override = state.get("_validation_config_override")
    if override is not None and not state.get("_validation_step_visited"):
        # A duplicate carries the source run's test-set recipe; the picker was
        # never shown for it, so the override is the only place it can live. Once
        # the operator opens the step, the fields below are their own answer.
        return override
    try:
        from uwazi_admin_agent.domain.validation import (
            VALIDATION_CUSTOM,
            VALIDATION_NONE,
            ValidationConfig,
            validation_property_name,
        )
    except Exception:  # noqa: BLE001 — an import failure must not block generation
        return None
    # The measured-property picker exists ONLY in the fill-a-property flow (see
    # ``_render_test_set_step``); no other goal keeps a test set, whatever a
    # stale widget from a previous render might still carry.
    if _validation_goal(state) != GOAL_FILL:
        return ValidationConfig(mode=VALIDATION_NONE)
    mode = str(state.get("_validation_mode_value") or getattr(state.get("_validation_mode"), "value", "none") or "none")
    if mode == VALIDATION_NONE:
        return ValidationConfig(mode=VALIDATION_NONE)
    property_widget = state.get("_validation_property")
    raw_value = getattr(property_widget, "value", "") or ""
    # The picker's value is the property NAME; when no picker exists (custom
    # prompt), the operator's typed name IS the value. The plain state key keeps
    # the choice across re-renders of the picker.
    property_name = str(raw_value).strip() or str(state.get("_validation_property_name") or "").strip()
    if not property_name:
        return ValidationConfig(mode=VALIDATION_NONE)
    answers = state.get("_guided_answers")
    templates = _validation_templates(answers)
    if not templates:
        # A duplicate inherited its population from the source run.
        templates = [
            str(name)
            for name in getattr(state.get("_validation_config_override"), "template_names", ()) or ()
            if str(name).strip()
        ]
    size_widget = state.get("_validation_size")
    raw_size = getattr(size_widget, "value", None)
    if raw_size is None:
        raw_size = state.get("_validation_size_value", 30)
    try:
        size = int(raw_size or 30)
    except (TypeError, ValueError):
        size = 30
    filter_widget = state.get("_validation_filter")
    raw_filter = getattr(filter_widget, "value", None)
    if raw_filter is None:
        raw_filter = state.get("_validation_filter_value", "")
    # The textarea is the operator's FILTER DESCRIPTION in custom mode. The
    # selector snippet itself is authored at generation time (see
    # ``build_validation_set``), so the operator never waits on an LLM turn
    # while filling the wizard.
    explanation = str(raw_filter or "").strip() if mode == VALIDATION_CUSTOM else ""
    # A wizard that carries a frozen sample (a duplicate) keeps it: the selector
    # that chose it travels too, and its members are named explicitly so the new
    # run scores the same population. A FRESH wizard has neither — the snippet is
    # authored at generation time (see ``build_validation_set``), so the operator
    # never waits on an LLM turn while filling the form.
    selector_code = str(state.get("_validation_selector_code") or "") if mode == VALIDATION_CUSTOM else ""
    shared_ids = (
        [str(shared_id) for shared_id in state.get("_validation_shared_ids") or [] if str(shared_id).strip()]
        if mode != VALIDATION_NONE
        else []
    )
    return ValidationConfig(
        mode=mode,
        property_name=validation_property_name(property_name),
        property_type=_validation_property_type(state, property_name),
        template_names=templates,
        sample_size=max(1, min(200, size)),
        selector_code=selector_code,
        selector_explanation=explanation,
        shared_ids=shared_ids,
        language=_wizard_default_language(answers),
        # What the wizard DECLARED as input. The measured property is hidden from
        # the script's reads unless it is one of these: "read the property you
        # are proposing" is only legitimate when the operator asked for it.
        input_properties=_declared_input_properties(answers, property_name),
    )


def _declared_input_properties(answers: Any, measured_property: str) -> list[str]:
    """The properties the guided answers declared as INPUTS (pure).

    The source metadata properties the operator checked, and nothing else.

    The target property is deliberately NOT added for the "only entities where
    it is currently empty" scope: that is a SCOPE, not a permission to read the
    target. The script's job is to PROPOSE the value, and a validation entity
    always carries one (an empty one cannot be scored). Letting the empty-scope
    script read the stored value hid every held-out entity from the scoring pass
    — it proposed nothing for all of them — and made the score measure whether
    the entity was already filled rather than whether the extraction is right.
    """
    declared = [str(name).strip() for name in getattr(answers, "metadata_properties", ()) or () if str(name).strip()]
    del measured_property
    return sorted(set(declared))


def _validation_property_type(state: dict[str, Any], name: str) -> str:
    """The measured property's Uwazi type (pure-ish, best effort)."""
    schema = state.get("_guided_schema")
    try:
        from uwazi_admin_agent.domain.guided_task import property_type

        return property_type(schema, name)
    except Exception:  # noqa: BLE001
        return ""


def _wizard_default_language(answers: Any) -> str:
    """The entity ROW locale the sample is read in: the language being updated.

    The test set freezes the value stored in the row the task writes to, so a
    specific output language wins; the input language (what the document is
    read in) only decides the row when the output follows the source.
    """
    output = list(getattr(answers, "output_languages", ()) or ())
    if output and str(getattr(answers, "output_language_mode", "")) == OUTPUT_SPECIFIC:
        return str(output[0])
    languages = list(getattr(answers, "input_languages", ()) or ())
    return str(languages[0]) if languages else "en"


def _wizard_step_caps(state: dict[str, Any], stepper: Any, dialog: Any) -> None:
    """One step that decides whether the finished task may use the AI, and its cap.

    Written for an operator who has never seen a script or an LLM call: a single
    checkbox answers "may the task infer values with an LLM?", and only when it
    is checked does the money limit appear. Unchecked (the DEFAULT) means the
    generated script must compute its values from the data itself (the ledger's
    deny-by-default zero budget), so no per-item AI spend can happen. The
    request-count backstop is derived here — the operator only sets money.
    """
    with ui.step(name="caps", title="AI allowance", icon="payments"):
        ui.label("May the task use the AI?").classes("text-body1 q-mb-md")
        with ui.card().classes("w-full"):
            # Unchecked by default: the generated script derives its values from
            # the data itself unless the operator deliberately opts in.
            state["_caps_use_llm"] = ui.checkbox(
                "Let the task use an LLM to infer the values",
                value=False,
                on_change=lambda _event: _sync_caps_fields(state),
            ).classes("text-body1")
            ui.label(
                "Unchecked: the task must derive its values from the data itself — no AI calls after generation. "
                "Checked: the generated script may call the AI, and every call is bounded by the cap below."
            ).classes("text-body2 text-grey-7 q-mt-xs")
            with ui.column().classes("w-full") as cost_fields:
                state["_caps_cost_fields"] = cost_fields
                state["_caps_cost_input"] = ui.number(
                    "Maximum spend ($)",
                    value=WIZARD_DEFAULT_AI_ALLOWANCE,
                    min=0.0,
                    step=0.5,
                    format="%.2f",
                ).classes("w-full")
                ui.label("A typical summary-style task over a few hundred items costs well under a dollar.").classes(
                    "text-body2 text-grey-7 q-mt-sm"
                )
        ui.label(
            "The next step's AI check is informed by this allowance: the LLM tailors its "
            "questions (and its restatement) to what the task can actually afford."
        ).classes("text-body2 text-grey-7 q-mt-md")
        with ui.row().classes("q-mt-lg w-full justify-end"):
            ui.button("Cancel", on_click=lambda: dialog.close()).props("color=grey-7 flat")
            ui.button("Back", on_click=lambda: _wizard_caps_back(state)).props("color=grey-7 flat")
            ui.button(
                "Next",
                icon="arrow_forward",
                on_click=lambda: stepper.set_value("understand"),
            ).props("color=primary")
    _sync_caps_fields(state)


def _sync_caps_fields(state: dict[str, Any]) -> None:
    """Show the spend cap only while the task is allowed to use the AI."""
    fields = state.get("_caps_cost_fields")
    checkbox = state.get("_caps_use_llm")
    if fields is None or fields.is_deleted:
        return
    fields.set_visibility(bool(getattr(checkbox, "value", False)))


def _wizard_caps_back(state: dict[str, Any]) -> None:
    """Back from the AI allowance: the test set (fill flow only) or the prompt."""
    if _validation_goal(state) == GOAL_FILL:
        state["_stepper"].set_value("test_set")
        return
    _wizard_back_to_prompt_step(state)


def _wizard_step_generate(state: dict[str, Any], dialog: Any, stepper: Any) -> None:
    with ui.step(name="generate", title="Generate", icon="auto_awesome"):
        ui.label("Review and generate the task.").classes("text-body1 q-mb-md")
        ui.label().bind_text_from(state, "name", backward=lambda v: f"Run: {v}").classes("text-h6")
        # A DUPLICATE opens on this step, so this is the only place the settings it
        # carried over are visible before Generate: the approved AI allowance and
        # the test set it will score against. Without it the copy went straight to
        # "generate this prompt" and the operator could not tell whether those
        # settings had travelled.
        state["_generate_settings"] = ui.column().classes("w-full q-mb-md")
        _render_generate_settings(state)
        ui.label("Final prompt (sent to the LLM)").classes("text-subtitle1 text-grey-7 q-mt-md")
        prompt_display = (
            ui.textarea().classes("w-full font-mono").props("readonly outlined autogrow").style("min-height: 120px")
        )
        state["_generate_prompt_display"] = prompt_display
        with ui.row().classes("q-mt-lg w-full justify-end"):
            ui.button("Cancel", on_click=lambda: dialog.close()).props("color=grey-7 flat")
            ui.button("Back", on_click=lambda: _wizard_generate_back(state)).props("color=grey-7 flat")
            ui.button(
                "Generate",
                icon="auto_awesome",
                on_click=lambda: _wizard_generate(state, dialog),
            )
    _render_generate_settings(state)


def _render_generate_settings(state: dict[str, Any]) -> None:
    """Paint the allowance and the test set the task will actually use (pure-ish).

    Read from the same builders Generate submits (``_wizard_budget`` and
    ``_wizard_validation_config``), so the line can never advertise a setting the
    run was not created with.
    """
    holder = state.get("_generate_settings")
    if holder is None or getattr(holder, "is_deleted", False):
        return
    holder.clear()
    with holder:
        budget = _wizard_budget(state)
        if budget.max_cost > 0 or budget.max_requests > 0:
            ui.label(f"AI allowance: ${budget.max_cost:.2f} · {budget.max_requests:,} request(s)").classes(
                "text-body2 text-grey-8"
            )
        else:
            ui.label("AI allowance: none — the task must compute its values from the data itself").classes(
                "text-body2 text-grey-8"
            )
        config = _validation_config_override_or_state(state)
        if config is None or str(getattr(config, "mode", "none")) == "none":
            ui.label("Test set: none — the task will not be scored").classes("text-body2 text-grey-8")
            return
        measured = str(getattr(config, "property_name", "") or "")
        size = getattr(config, "sample_size", 0)
        how = "chosen by a custom rule" if str(getattr(config, "mode", "")) == "custom" else "picked automatically"
        noun = "entity" if size == 1 else "entities"
        ui.label(f"Test set: {size} {noun} ({how}), measuring {measured or 'a property'}").classes("text-body2 text-grey-8")


def _validation_config_override_or_state(state: dict[str, Any]) -> Any:
    """The recipe Generate would submit, without the picker widgets being built.

    ``_wizard_validation_config`` is the ONE builder Generate uses; it already
    prefers an inherited recipe until the operator opens the step, and reads the
    step's own state keys otherwise — so a duplicate (whose widgets were never
    built) still reports what it will submit.
    """
    if _validation_goal(state) != GOAL_FILL and state.get("_validation_config_override") is None:
        return None
    return _wizard_validation_config(state)


def _start_generation(
    name: str,
    prompt: str,
    user: str,
    password: str,
    instance: str,
    validated_prompt: str | None = None,
    claimed: bool = False,
    budget: LlmBudget | None = None,
    validation_config: Any | None = None,
) -> None:
    """Register the in-flight placeholder + notification and launch generation.

    Shared by the new-task wizard and the retry path on a ``generation_failed``
    run so both produce the identical toast/table/background-task flow.

    Generation takes only a per-run claim: it writes the run's folder and the
    review list but never Uwazi, so it must not block (or be blocked by) a
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
        _do_generate(
            name,
            prompt,
            user,
            password,
            instance,
            validated_prompt,
            budget,
            notification,
            validation_config,
        ),
        name=f"generate {name}",
    )


def _wizard_generate(state: dict[str, Any], dialog: Any) -> None:
    name = state.get("name", "")
    prompt = state.get("prompt", "")
    if not name or not prompt:
        ui.notify("Name and prompt are required", type="warning")
        return
    # Sync the display one last time so what the operator saw is what is sent.
    _refresh_generate_prompt(state)
    budget = _wizard_budget(state)
    final_prompt = state.get("final_prompt") or prompt
    dialog.close()
    _confirm_generation(name, prompt, final_prompt, budget, validation_config=_wizard_validation_config(state))


def _script_caps_model() -> str:
    """The configured model name (falls back to a placeholder when unreadable)."""
    try:
        from uwazi_agent.configuration import MODEL

        return MODEL
    except Exception:  # noqa: BLE001 — a missing/renamed constant must not break the wizard
        return "(model not configured)"


def _apply_budget_prefill(state: dict[str, Any], budget: LlmBudget) -> None:
    """Write a duplicate's approved allowance into the caps controls (pure-ish).

    The wizard's caps step is skipped when a duplicate opens, so the widgets
    must carry the SOURCE run's decision or ``_wizard_budget`` would fall back
    to deny-by-default and the copy would silently lose its LLM allowance.
    """
    checkbox = state.get("_caps_use_llm")
    cost_input = state.get("_caps_cost_input")
    if checkbox is None or cost_input is None:
        return
    checkbox.value = budget.max_cost > 0 or budget.max_requests > 0
    # A requests-only ceiling (legacy/custom budget) must keep a positive cost
    # so the wizard's default-backstop rule does not collapse it to deny-all.
    cost_input.value = float(
        budget.max_cost if budget.max_cost > 0 else (PREDICTION_DEFAULT_MAX_COST if budget.max_requests > 0 else 0.0)
    )
    _sync_caps_fields(state)


def _wizard_budget(state: dict[str, Any]) -> LlmBudget:
    """The approved budget for the script's llm_complete/llm_map calls (caps step).

    The checkbox is the opt-in: unchecked means the task may not call the AI at
    all, so the budget is the deny-by-default pair (0, 0) and the ledger aborts
    the script's first LLM call. Checked, the operator sets ONLY money;
    ``with_default_backstops`` derives the request-count backstop the ledger
    still enforces (any positive cost fills it with its standard default). An
    empty/invalid cost input falls back to the configured default.
    """
    use_llm_widget = state.get("_caps_use_llm")
    use_llm = bool(getattr(use_llm_widget, "value", False)) if use_llm_widget is not None else False
    cost_widget = state.get("_caps_cost_input")
    if not use_llm:
        cost: float | None = 0.0
    else:
        cost = _cap_float(cost_widget.value) if cost_widget is not None else None
    max_cost, max_requests = with_default_backstops(
        WIZARD_DEFAULT_AI_ALLOWANCE if cost is None else cost,
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


def _confirm_generation(
    name: str,
    prompt: str,
    validated_prompt: str | None,
    budget: LlmBudget,
    validation_config: Any | None = None,
) -> None:
    """A plain go/no-go confirm, then launch generation.

    Generation itself is bounded by the built-in caps (``MAX_LLM_CALLS``
    requests via pydantic-ai's ``UsageLimits``, ``LLM_MAX_OUTPUT_TOKENS`` per
    call). ``budget`` — approved in the wizard's caps step — is the HARD ceiling
    on the generated script's own llm_complete/llm_map calls, enforced during
    the planning pass that fills the review list.
    """
    ui.notification(
        "Generating the script — the planning pass runs right after and fills the suggestions under the approved LLM caps.",
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
        validation_config=validation_config,
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
    validation_config: Any | None = None,
) -> None:
    def _on_stage(stage: str) -> None:
        """Publish the generation stage on the run's claim.

        Called from the WORKER thread, so it touches only the thread-safe
        claim registry: no NiceGUI client context, no outbox write. The tasks
        table's 5s refresh (and every explicit broadcast) reads the note and
        shows it beside the row's status.
        """
        operation_gate.set_claim_note(instance, name, stage)

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
                on_stage=_on_stage,
                validation_config=validation_config,
            ),
        )
        _broadcast_notify(
            instance,
            f"Run {name!r} generated — the planning pass produced its suggestions.",
            type="positive",
        )
    except Exception as exc:  # noqa: BLE001
        _notify_error(instance, "Generation failed", str(exc))
    finally:
        operation_gate.release_run(instance, name, "creating")
        if notification is not None:
            notification.dismiss()
        _broadcast_rows(instance)
