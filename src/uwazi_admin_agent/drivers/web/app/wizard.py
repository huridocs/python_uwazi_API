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
    SCRIPT_DEFAULT_MAX_REQUESTS,
)
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
from .guided_form import build_guided_step, ensure_guided_form
from .state import _broadcast_notify, _busy_run_text, _notify_error, _session_ended

# How the details step produces the prompt: typed by the operator, or assembled
# from the guided questions. Both paths converge on the same prompt field, so
# the validate/generate steps cannot tell them apart.
_PROMPT_MODE_FREE = "free"
_PROMPT_MODE_GUIDED = "guided"


def _new_task_wizard(prefill_name: str = "", prefill_prompt: str = "", prefill_validated_prompt: str | None = None) -> None:
    """Multi-step dialog to create + generate a run.

    ``prefill_name``/``prefill_prompt`` pre-populate the wizard (used by the
    Duplicate menu action); when a prefill is present the wizard opens
    directly on the Generate review step (validation is skipped).

    ``prefill_validated_prompt`` is the source run's FINAL prompt — the original
    request plus the validation step's resolved clarifications (and, after a
    chat Run, the whole conversation). A duplicate must generate from that, not
    from the bare original: dropping the clarifications is what made a second
    generation plan a weaker request and get refused. ``prefill_prompt`` stays
    the original request, which is what the manifest records as ``prompt``.
    """
    base_prompt = (prefill_validated_prompt or prefill_prompt).strip() or prefill_prompt
    state: dict[str, Any] = {"name": prefill_name, "prompt": prefill_prompt, "final_prompt": base_prompt}
    # What the clarification pass is shown, and what a re-validation's answers
    # get folded back into. For a fresh wizard it is the operator's request; for
    # a duplicate it is the source run's already-resolved prompt.
    state["_clarify_base"] = base_prompt
    if prefill_name or prefill_prompt:
        # The generate step recomputes from ``_prompt_base``; set it BEFORE the
        # step is built or the first repaint would drop the clarifications.
        state["_prompt_base"] = base_prompt
        state["validated_prompt"] = base_prompt

    with ui.dialog() as dialog, ui.card().classes("w-full max-w-4xl"):
        dialog.props("persistent")
        modal_header(dialog, "New task")
        with ui.stepper().classes("w-full") as stepper:
            state["_dialog"] = dialog
            state["_stepper"] = stepper
            _wizard_step_details(state, stepper, dialog, prefill_name=prefill_name, prefill_prompt=prefill_prompt)
            _wizard_step_guided(state, dialog)
            _wizard_step_validate(state, stepper, dialog)
            _wizard_step_generate(state, dialog, stepper)
            # The stepper header is clickable: landing on the questions step by
            # any route must load its form at most once.
            stepper.on_value_change(
                lambda: ensure_guided_form(state) if stepper.value == "guided" else None
            )
        dialog.open()
        if prefill_name or prefill_prompt:
            stepper.set_value("generate")
            _show_generate_prompt(state)


def _wizard_step_details(
    state: dict[str, Any],
    stepper: Any,
    dialog: Any,
    prefill_name: str = "",
    prefill_prompt: str = "",
) -> None:
    """Step 1: the run's name, plus how its prompt is produced.

    Two ways in: write the prompt directly, or answer the guided questions
    (:mod:`uwazi_admin_agent.drivers.web.app.guided_form`), which assemble the
    prompt heuristically into this same panel. The panels share one prompt, and
    the validate → generate flow after them is identical — a guided prompt is
    just a prompt that was written for the operator.
    """
    with ui.step(name="details", title="Task", icon="edit"):
        ui.label("Name the run (a new folder will be created under data/runs/) and describe the task.").classes(
            "text-body1 q-mb-md"
        )
        name_input = ui.input(
            "Run name",
            placeholder="e.g. merge-entities-2026",
            value=prefill_name,
            validation={"Required": lambda v: bool(v and v.strip())},
        ).classes("w-full text-h6")
        mode_input = ui.radio(
            {
                _PROMPT_MODE_FREE: "Write the prompt myself",
                _PROMPT_MODE_GUIDED: "Answer a few questions (no LLM)",
            },
            value=_PROMPT_MODE_FREE,
        ).props("dense").classes("q-mt-md")
        prompt_input = (
            ui.textarea(
                "Prompt",
                placeholder="e.g. Merge duplicate entities sharing the title 'X'...",
                value=prefill_prompt,
                validation={"Required": lambda v: bool(v and v.strip())},
            )
            .classes("w-full q-mt-md")
            .props("autogrow input-style='min-height: 200px;'")
            .style("width: 100%")
        )
        state["_details_name_input"] = name_input
        state["_details_prompt_input"] = prompt_input
        state["_details_mode_input"] = mode_input
        mode_input.on_value_change(lambda: prompt_input.set_visibility(mode_input.value == _PROMPT_MODE_FREE))
        with ui.row().classes("q-mt-lg w-full justify-end"):
            ui.button("Cancel", on_click=lambda: dialog.close()).props("color=grey-7 flat")
            ui.button("Next", on_click=lambda: _wizard_details_next(state, name_input, prompt_input, stepper))


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


def _wizard_details_next(state: dict[str, Any], name_input: Any, prompt_input: Any, stepper: Any) -> None:
    """Continue from the details step: to the questions, or on to validate."""
    if _valid_run_name(state, name_input) is None:
        return
    if (state.get("_details_mode_input") is not None) and state["_details_mode_input"].value == _PROMPT_MODE_GUIDED:
        state["_prompt_source"] = _PROMPT_MODE_GUIDED
        stepper.set_value("guided")
        ensure_guided_form(state)
        return
    prompt = (prompt_input.value or "").strip()
    if not prompt:
        ui.notify("Prompt is required", type="warning")
        return
    state["_prompt_source"] = _PROMPT_MODE_FREE
    _advance_with_prompt(state, prompt, stepper)


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
    """Accept the assembled prompt from the questions step and continue the flow.

    The stepper header can reach this step without passing the details panel's
    Next, so the run name is validated HERE too: an unchecked name would either
    be empty or overwrite an existing run's folder.
    """
    name_input = state.get("_details_name_input")
    if name_input is None or _valid_run_name(state, name_input) is None:
        if name_input is not None:
            state["_stepper"].set_value("details")
        return
    state["_prompt_source"] = _PROMPT_MODE_GUIDED
    if state.get("_details_mode_input") is not None:
        state["_details_mode_input"].value = _PROMPT_MODE_GUIDED
    prompt_input = state.get("_details_prompt_input")
    if prompt_input is not None:
        prompt_input.value = prompt
    _advance_with_prompt(state, prompt, state["_stepper"])


def _wizard_back_to_details(state: dict[str, Any]) -> None:
    """Back from validate/generate: the questions step when its prompt is in use."""
    state["_stepper"].set_value("guided" if state.get("_prompt_source") == _PROMPT_MODE_GUIDED else "details")


def _guided_back_to_details(state: dict[str, Any]) -> None:
    """Return from the questions to the details panel, keeping the chosen mode."""
    state["_stepper"].set_value("details")


def _guided_switch_to_free_text(state: dict[str, Any]) -> None:
    """Give up on the questions and edit the prompt directly."""
    state["_prompt_source"] = _PROMPT_MODE_FREE
    if state.get("_details_mode_input") is not None:
        state["_details_mode_input"].value = _PROMPT_MODE_FREE
    state["_stepper"].set_value("details")


def _wizard_step_guided(state: dict[str, Any], dialog: Any) -> None:
    build_guided_step(
        state,
        dialog,
        on_submit=lambda prompt, _answers: _guided_prompt_ready(state, prompt),
        on_back=lambda: _guided_back_to_details(state),
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
            ui.button("Back", on_click=lambda: _wizard_back_to_details(state)).props("color=grey-7 flat")
            ui.button("Skip", on_click=lambda: _wizard_skip(state)).props("color=grey-7 flat")
            ui.button("Validate", icon="help", on_click=lambda: _start_validation(state)).props("color=primary")


def _start_validation(state: dict[str, Any]) -> None:
    """Kick off the prompt-validation LLM call and show a loading state."""
    if not (state.get("prompt") or "").strip():
        # The stepper header can jump straight here: validating nothing would
        # ask the LLM to interpret an empty request.
        ui.notify("Write or build a prompt first", type="warning")
        _wizard_back_to_details(state)
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
            ui.button("Back", on_click=lambda: _wizard_back_to_details(state)).props("color=grey-7 flat")
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
            ui.button("Back", on_click=lambda: _wizard_back_to_details(state)).props("color=grey-7 flat")
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
        _wizard_back_to_details(state)
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
    """Back from the generate step: to validate (if it ran) or to the prompt step."""
    if state.get("_validation_done"):
        target = "validate"
    else:
        target = "guided" if state.get("_prompt_source") == _PROMPT_MODE_GUIDED else "details"
    state["_stepper"].set_value(target)


def _wizard_step_generate(state: dict[str, Any], dialog: Any, stepper: Any) -> None:
    with ui.step(name="generate", title="Generate", icon="auto_awesome"):
        ui.label("Review and generate the migration script.").classes("text-body1 q-mb-md")
        ui.label().bind_text_from(state, "name", backward=lambda v: f"Run: {v}").classes("text-h6")
        ui.label("Final prompt (sent to the LLM)").classes("text-subtitle1 text-grey-7 q-mt-md")
        prompt_display = (
            ui.textarea().classes("w-full font-mono").props("readonly outlined autogrow").style("min-height: 120px")
        )
        state["_generate_prompt_display"] = prompt_display
        ui.separator()
        with ui.column().classes("w-full q-mt-sm"):
            ui.label("LLM caps for the script's tool calls").classes("text-subtitle1 text-bold")
            ui.label(
                "The generated script may call the LLM to compute values (per entity). "
                "These HARD caps apply ONLY to those calls — never to script generation itself. "
                "A script that computes everything in pure Python costs nothing under them."
            ).classes("text-body2 text-grey-7")
            with ui.row().classes("w-full items-center gap-2"):
                state["_caps_cost_input"] = ui.number(
                    "Max cost ($)",
                    value=SCRIPT_DEFAULT_MAX_COST,
                    min=0.0,
                    step=0.5,
                    format="%.2f",
                    on_change=lambda: _caps_opt_in(state, "cost"),
                ).classes("grow")
                state["_caps_requests_input"] = ui.number(
                    "Max calls (backstop)",
                    value=SCRIPT_DEFAULT_MAX_REQUESTS,
                    min=0,
                    step=1,
                    on_change=lambda: _caps_opt_in(state, "requests"),
                ).classes("grow")
            ui.label(
                "Default is 0 on both: the script's LLM calls are disabled — the pass aborts cleanly on the "
                "first one. Raise EITHER cap to opt in; the other is filled with its standard backstop so the "
                "allowance is actually usable."
            ).classes("text-body2 text-grey-7")
            ui.label("The pass aborts cleanly at the caps; rows computed before the halt are kept.").classes(
                "text-body2 text-grey-7"
            )
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


def _caps_opt_in(state: dict[str, Any], changed: str) -> None:
    """Fill the missing backstop when the operator RAISES one cap (UI mirror).

    Both caps are enforced by the planning pass's ledger, so a lone positive cap
    would deny the script's first LLM call. This writes the standard backstop
    into the visible field, so the operator sees the budget that will apply.
    Only a RAISE fills the sibling; clearing a field back to 0 is left alone (the
    operator can zero both to disable LLM calls), so the mirror never fights a
    deliberate clear.
    """
    cost_widget = state.get("_caps_cost_input")
    requests_widget = state.get("_caps_requests_input")
    if cost_widget is None or requests_widget is None:
        return
    cost = _cap_float(cost_widget.value)
    requests = _cap_int(requests_widget.value)
    if changed == "cost" and cost is not None and cost > 0 and (requests is None or requests <= 0):
        requests_widget.value = with_default_backstops(
            cost,
            0,
            default_cost=PREDICTION_DEFAULT_MAX_COST,
            default_requests=PREDICTION_DEFAULT_MAX_REQUESTS,
        )[1]
    elif changed == "requests" and requests is not None and requests > 0 and (cost is None or cost <= 0):
        cost_widget.value = with_default_backstops(
            0,
            requests,
            default_cost=PREDICTION_DEFAULT_MAX_COST,
            default_requests=PREDICTION_DEFAULT_MAX_REQUESTS,
        )[0]


def _wizard_budget(state: dict[str, Any]) -> LlmBudget:
    """The approved budget for the script's llm_complete/llm_map calls (wizard caps step).

    0 on BOTH is a real, operator-visible value — the deny-by-default prefill —
    so it is kept as-is (a 0 request cap aborts the pass on its first LLM call).
    Raising either cap opts in: the other dimension is filled with its standard
    backstop, because the ledger enforces both and a lone cap would otherwise
    deny the very call the operator just allowed. Only an empty/invalid input
    falls back to the configured default.
    """
    cost = _cap_float(state["_caps_cost_input"].value) if state.get("_caps_cost_input") is not None else None
    requests = _cap_int(state["_caps_requests_input"].value) if state.get("_caps_requests_input") is not None else None
    max_cost, max_requests = with_default_backstops(
        SCRIPT_DEFAULT_MAX_COST if cost is None else cost,
        SCRIPT_DEFAULT_MAX_REQUESTS if requests is None else requests,
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


def _cap_int(value: Any) -> int | None:
    """Coerce a caps input to a non-negative int (None/invalid → None = 'use the default')."""
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


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
