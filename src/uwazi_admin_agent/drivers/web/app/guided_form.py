"""One-question-at-a-time guided form for the New Task wizard.

The goal is selected in the preceding wizard step. This form derives its next
question from the answers already given and assembles the resulting prompt
without an LLM. Instance schema loading happens off the event loop.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from loguru import logger
from nicegui import background_tasks, ui

from uwazi_admin_agent.domain.guided_flow import (
    ADDITIONAL_RULES_QUESTION,
    CUSTOM_DESCRIPTION_QUESTION,
    DOCUMENT_GUIDANCE_QUESTION,
    EXAMPLES_QUESTION,
    EXISTING_VALUE_MODE_QUESTION,
    EXISTING_VALUE_RULE_QUESTION,
    FALLBACK_MODE_QUESTION,
    FALLBACK_VALUE_QUESTION,
    GENERATION_QUESTION,
    FILL_RULES_QUESTION,
    INPUT_LANGUAGE_MODE_QUESTION,
    INPUT_LANGUAGES_QUESTION,
    METADATA_PROPERTIES_QUESTION,
    OUTPUT_LANGUAGE_MODE_QUESTION,
    OUTPUT_LANGUAGES_QUESTION,
    REVIEW_QUESTION,
    SCOPE_FILTER_QUESTION,
    SCOPE_QUESTION,
    SOURCES_QUESTION,
    TARGET_PROPERTY_QUESTION,
    TEMPLATE_QUESTION,
    guided_question_ids,
    next_question_id,
    previous_question_id,
    question_error,
)
from uwazi_admin_agent.domain.guided_task import (
    EXISTING_VALUE_LABELS,
    FALLBACK_BLANK,
    FALLBACK_LABELS,
    FALLBACK_VALUE,
    GOAL_CLASSIFY,
    GOAL_LABELS,
    INPUT_LANGUAGE_LABELS,
    INPUT_LANGUAGE_MODES,
    OUTPUT_LANGUAGE_LABELS,
    OUTPUT_LANGUAGE_MODES,
    SCOPE_LABELS,
    SCOPES,
    SOURCE_LABELS,
    SOURCE_METADATA,
    SOURCES,
    GuidedAnswers,
    WizardSchema,
    assemble_prompt,
    language_choices,
    language_label_map,
    metadata_property_choices,
    property_choices,
    validate_answers,
)
from uwazi_admin_agent.drivers.web.run_service.wizard_schema import load_wizard_schema
from uwazi_admin_agent.drivers.web.session import credentials_for_instance, page_instance_url

_QUESTION_TITLES = {
    TEMPLATE_QUESTION: "Which templates should be analyzed?",
    CUSTOM_DESCRIPTION_QUESTION: "What should the task do?",
    TARGET_PROPERTY_QUESTION: "Which property should receive the suggestion?",
    EXISTING_VALUE_MODE_QUESTION: "What if the property already has a value?",
    EXISTING_VALUE_RULE_QUESTION: "What custom rule should apply to existing values?",
    FALLBACK_MODE_QUESTION: "What if no topic clearly applies?",
    FALLBACK_VALUE_QUESTION: "What fallback value should be suggested?",
    SCOPE_QUESTION: "Which entities should be included?",
    SCOPE_FILTER_QUESTION: "What filter or condition selects the entities?",
    SOURCES_QUESTION: "Which information should be used as input?",
    METADATA_PROPERTIES_QUESTION: "Which metadata properties should be read?",
    INPUT_LANGUAGE_MODE_QUESTION: "Which input language should be read?",
    INPUT_LANGUAGES_QUESTION: "Which primary input languages should be used?",
    OUTPUT_LANGUAGE_MODE_QUESTION: "How should multilingual outputs be generated?",
    OUTPUT_LANGUAGES_QUESTION: "Which output languages should be generated?",
    GENERATION_QUESTION: "How should the value be generated?",
    REVIEW_QUESTION: "Review the assembled prompt",
}


def _radio(
    state: dict[str, Any],
    options: dict[str, str],
    value: str,
    *,
    on_change: Callable[[], None] | None = None,
) -> Any:
    state["_guided_active_radio"] = radio = ui.radio(options, value=value).classes("wizard-options w-full")
    state["_guided_radio_value"] = value
    radio.on_value_change(lambda event: _radio_changed(state, event.value, on_change))
    return radio


def _radio_changed(state: dict[str, Any], value: str, on_change: Callable[[], None] | None) -> None:
    state["_guided_radio_value"] = value
    if on_change is not None:
        on_change()


def build_guided_step(
    state: dict[str, Any],
    dialog: Any,
    *,
    on_submit: Callable[[str, GuidedAnswers], None],
    on_back: Callable[[], None],
    on_free_text: Callable[[], None],
) -> None:
    """Create the guided step; its first question loads lazily on entry."""
    with ui.step(name="guided", title="Questions", icon="quiz"):
        state["_guided_submit"] = on_submit
        state["_guided_dialog"] = dialog
        state["_guided_free_text"] = on_free_text
        state["_guided_back_to_details"] = on_back
        state["_guided_container"] = ui.column().classes("w-full")


def start_guided_flow(state: dict[str, Any], goal: str) -> None:
    """Start or resume a flow for ``goal`` without discarding existing answers."""
    answers = state.get("_guided_answers") or GuidedAnswers()
    if state.get("_guided_goal") != goal:
        answers = GuidedAnswers(goal=goal)
        state["_guided_answers"] = answers
    state["_guided_goal"] = goal
    state["_guided_question"] = guided_question_ids(answers)[0]
    ensure_guided_form(state)


def ensure_guided_form(state: dict[str, Any], *, refresh: bool = False) -> None:
    """Load the instance schema once, then render the current question."""
    if state.get("_guided_loading") or (state.get("_guided_built") and not refresh):
        _render_question(state)
        return
    if refresh:
        state.pop("_guided_built", None)
    container = state.get("_guided_container")
    if container is None:
        return
    state["_guided_loading"] = True
    _render_loading(container)
    background_tasks.create(_load_and_render(state, refresh=refresh), name="guided task schema")


def _render_loading(container: Any) -> None:
    container.clear()
    with container, ui.row().classes("w-full items-center gap-2 q-my-lg"):
        ui.spinner(size="lg")
        ui.label("Reading the instance's templates and languages...").classes("text-body1")


async def _load_and_render(state: dict[str, Any], *, refresh: bool = False) -> None:
    instance = page_instance_url()
    credentials = credentials_for_instance(instance)
    if credentials is None:
        _render_schema_error(state, RuntimeError("your session ended — log in again"))
        return
    try:
        schema = await asyncio.to_thread(
            load_wizard_schema,
            instance,
            credentials.user,
            credentials.password,
            refresh=refresh,
        )
    except Exception as exc:  # noqa: BLE001 — free-text entry remains available
        logger.warning("could not read the instance schema for the guided wizard: {}", exc)
        _render_schema_error(state, exc)
        return
    state["_guided_loading"] = False
    state["_guided_schema"] = schema
    state["_guided_built"] = True
    _render_question(state)


def _render_schema_error(state: dict[str, Any], exc: Exception) -> None:
    container = state["_guided_container"]
    state["_guided_loading"] = False
    container.clear()
    with container:
        ui.label("Could not read the instance's templates").classes("text-h6 text-negative")
        ui.label(str(exc)).classes("text-body2 q-mb-sm")
        ui.label("Retry, or describe the task in your own words instead.").classes("text-body2 text-grey-7")
        with ui.row().classes("q-mt-md w-full justify-between"):
            ui.button("Back", on_click=lambda: state["_guided_back_to_details"]()).props("color=grey-7 flat")
            with ui.row():
                ui.button("Use custom task", on_click=lambda: state["_guided_free_text"]()).props("color=grey-7 flat")
                ui.button("Retry", icon="refresh", on_click=lambda: _retry(state)).props("color=primary")


def _retry(state: dict[str, Any]) -> None:
    state["_guided_loading"] = False
    ensure_guided_form(state, refresh=True)


def _multi_picker(
    label: str,
    choices: list[tuple[str, str]],
    *,
    allow_custom: bool,
    placeholder: str,
) -> Any:
    if choices:
        return (
            ui.select(
                dict(choices),
                label=label,
                multiple=True,
                with_input=True,
                clearable=True,
                new_value_mode="add-unique" if allow_custom else None,
            )
            .props("outlined use-chips")
            .classes("w-full")
        )
    return ui.input(label, placeholder=placeholder).classes("w-full")


def _single_picker(label: str, choices: list[tuple[str, str]], *, placeholder: str) -> Any:
    if choices:
        return (
            ui.select(dict(choices), label=label, with_input=True, clearable=True, new_value_mode="add-unique")
            .props("outlined")
            .classes("w-full")
        )
    return ui.input(label, placeholder=placeholder).classes("w-full")


def _render_question(state: dict[str, Any]) -> None:
    """Render one question; all other answers remain in ``_guided_answers``."""
    if not state.get("_guided_built"):
        return
    answers: GuidedAnswers = state["_guided_answers"]
    questions = guided_question_ids(answers)
    current = state.get("_guided_question")
    if current not in questions:
        current = questions[0]
        state["_guided_question"] = current
    index = questions.index(current)
    container = state["_guided_container"]
    container.clear()
    with container:
        ui.label(GOAL_LABELS[answers.goal]).classes("text-overline text-primary")
        ui.label(f"Question {index + 1} of {len(questions)}").classes("text-caption text-grey-7")
        ui.linear_progress(value=(index + 1) / len(questions), show_value=False).classes("w-full q-mb-lg")
        ui.label(_QUESTION_TITLES[current]).classes("text-h6 q-mb-md")
        with ui.column().classes("w-full min-h-40"):
            _render_question_control(state, current, answers)
        with ui.row().classes("q-mt-lg w-full items-center justify-between no-wrap"):
            ui.button("Cancel", on_click=lambda: state["_guided_dialog"].close()).props("color=grey-7 flat")
            with ui.row().classes("no-wrap"):
                ui.button("Back", on_click=lambda: _back(state)).props("color=grey-7 flat")
                if current == REVIEW_QUESTION:
                    ui.button("Use prompt", icon="arrow_forward", on_click=lambda: _submit(state)).props("color=primary")
                else:
                    ui.button("Continue", icon="arrow_forward", on_click=lambda: _next(state)).props("color=primary")


_AUTO_ADVANCE_QUESTIONS = frozenset(
    {
        EXISTING_VALUE_MODE_QUESTION,
        FALLBACK_MODE_QUESTION,
        SCOPE_QUESTION,
        INPUT_LANGUAGE_MODE_QUESTION,
        OUTPUT_LANGUAGE_MODE_QUESTION,
    }
)


def _render_question_control(state: dict[str, Any], question: str, answers: GuidedAnswers) -> None:
    schema: WizardSchema = state["_guided_schema"]
    if question == TEMPLATE_QUESTION:
        state["_guided_draft_templates"] = list(answers.templates)
        ui.select(
            [template.name for template in schema.templates],
            label="Templates (leave empty for all)",
            value=list(answers.templates),
            multiple=True,
            with_input=True,
            clearable=True,
            new_value_mode="add-unique",
            on_change=lambda event: state.update(_guided_draft_templates=event.value or []),
        ).props("outlined use-chips").classes("w-full")
    elif question == CUSTOM_DESCRIPTION_QUESTION:
        _textarea(state, "custom_description", answers.custom_description, min_height=160)
    elif question == TARGET_PROPERTY_QUESTION:
        label = "Target property for the suggested topics" if answers.goal == GOAL_CLASSIFY else "Target property"
        placeholder = "e.g. topics" if answers.goal == GOAL_CLASSIFY else "e.g. incident_date, executive_summary"
        ui.label(
            "Select, multi-select and relationship properties accept thesaurus terms or linked entities."
            if answers.goal == GOAL_CLASSIFY
            else "The suggested value will appear in the review table before anything is written."
        ).classes("text-body2 text-grey-7 q-mb-md")
        widget = _single_picker(label, property_choices(schema, answers.templates, answers.goal), placeholder=placeholder)
        widget.value = answers.target_property or None
        widget.on_value_change(lambda event: state.update(_guided_draft_text=event.value or ""))
        state["_guided_draft_text"] = answers.target_property
    elif question == GENERATION_QUESTION:
        state["_guided_draft_rules"] = {
            FILL_RULES_QUESTION: answers.fill_rules,
            DOCUMENT_GUIDANCE_QUESTION: answers.document_guidance,
            ADDITIONAL_RULES_QUESTION: answers.additional_rules,
            EXAMPLES_QUESTION: answers.examples,
        }
        if answers.goal == GOAL_FILL:
            ui.label("Rules or formatting for the generated value").classes("text-subtitle1")
            _rules_textarea(
                state,
                FILL_RULES_QUESTION,
                answers.fill_rules,
                placeholder="e.g. Summarize the ruling in 3 concise bullet points.",
                min_height=120,
            )
        ui.label("Where does the relevant information live?").classes("text-subtitle1 q-mt-sm")
        ui.label("Optional — continue to use the default strategy").classes("text-body2 text-grey-7")
        _rules_textarea(
            state,
            DOCUMENT_GUIDANCE_QUESTION,
            answers.document_guidance,
            placeholder="e.g. Look at the first 2 pages for the header, date and author.",
        )
        ui.label("Any additional instructions or edge cases?").classes("text-subtitle1 q-mt-sm")
        ui.label("Optional").classes("text-body2 text-grey-7")
        _rules_textarea(
            state,
            ADDITIONAL_RULES_QUESTION,
            answers.additional_rules,
            placeholder="e.g. Keep legal terminology formal. Ignore footnotes.",
        )
        ui.label("Any examples of expected results?").classes("text-subtitle1 q-mt-sm")
        ui.label("Optional").classes("text-body2 text-grey-7")
        _rules_textarea(
            state,
            EXAMPLES_QUESTION,
            answers.examples,
            placeholder="e.g. 'Plaintiff was granted leave to appeal' -> 'Leave to appeal: granted'",
        )
    elif question == EXISTING_VALUE_MODE_QUESTION:
        _radio(state, EXISTING_VALUE_LABELS, answers.existing_value_mode, on_change=lambda: _next(state))
    elif question == EXISTING_VALUE_RULE_QUESTION:
        _input(state, "existing_value_rule", answers.existing_value_rule, "Custom rule")
    elif question == FALLBACK_MODE_QUESTION:
        _radio(
            state,
            {mode: FALLBACK_LABELS[mode] for mode in (FALLBACK_BLANK, FALLBACK_VALUE)},
            answers.fallback_mode,
            on_change=lambda: _next(state),
        )
    elif question == FALLBACK_VALUE_QUESTION:
        _input(state, "fallback_value", answers.fallback_value, "Fallback value")
    elif question == SCOPE_QUESTION:
        _radio(state, {scope: SCOPE_LABELS[scope] for scope in SCOPES}, answers.entity_scope, on_change=lambda: _next(state))
    elif question == SCOPE_FILTER_QUESTION:
        _input(state, "scope_filter", answers.scope_filter, "Filter or condition")
    elif question == SOURCES_QUESTION:
        state["_guided_draft_sources"] = set(answers.sources)
        ui.label("Choose all that apply.").classes("text-body2 text-grey-7 q-mb-sm")
        for source in SOURCES:
            ui.checkbox(
                SOURCE_LABELS[source],
                value=source in answers.sources,
                on_change=lambda event, source=source: _source_changed(state, source, bool(event.value)),
            ).classes("wizard-choice w-full")
    elif question == METADATA_PROPERTIES_QUESTION:
        _multi_answer(
            state,
            "metadata_properties",
            metadata_property_choices(schema, answers.templates),
            answers.metadata_properties,
            label="Metadata properties to read",
            placeholder="e.g. title, summary, description",
        )
    elif question == INPUT_LANGUAGE_MODE_QUESTION:
        ui.label("Applies when an entity has multiple language versions.").classes("text-body2 text-grey-7 q-mb-md")
        _radio(
            state,
            {mode: INPUT_LANGUAGE_LABELS[mode] for mode in INPUT_LANGUAGE_MODES},
            answers.input_language_mode,
            on_change=lambda: _next(state),
        )
    elif question == INPUT_LANGUAGES_QUESTION:
        _languages(state, "input_languages", answers.input_languages, "Primary input language(s)")
    elif question == OUTPUT_LANGUAGE_MODE_QUESTION:
        _radio(
            state,
            {mode: OUTPUT_LANGUAGE_LABELS[mode] for mode in OUTPUT_LANGUAGE_MODES},
            answers.output_language_mode,
            on_change=lambda: _next(state),
        )
    elif question == OUTPUT_LANGUAGES_QUESTION:
        _languages(state, "output_languages", answers.output_languages, "Output language(s)")
    elif question == REVIEW_QUESTION:
        ui.label("Go back to change any answer, or continue to validation with this prompt.").classes(
            "text-body2 text-grey-7 q-mb-md"
        )
        ui.textarea(value=assembled_prompt(state)).classes("w-full font-mono").props(
            "readonly outlined input-style='min-height: 300px;'"
        )


def _rules_textarea(
    state: dict[str, Any],
    field: str,
    value: str,
    *,
    placeholder: str = "",
    min_height: int = 90,
) -> None:
    """Textarea on the merged generation page; writes into the per-field drafts."""
    drafts = state.setdefault("_guided_draft_rules", {})
    drafts[field] = value
    widget = ui.textarea(value=value, placeholder=placeholder).classes("w-full")
    widget.props(f"autogrow outlined input-style='min-height: {min_height}px;'")
    widget.on_value_change(lambda event: drafts.__setitem__(field, event.value or ""))


def _input(state: dict[str, Any], field: str, value: str, label: str) -> None:
    state["_guided_draft_text"] = value
    widget = ui.input(label, value=value).classes("w-full")
    widget.on_value_change(lambda event: state.update(_guided_draft_text=event.value or ""))
    state["_guided_draft_field"] = field


def _textarea(
    state: dict[str, Any],
    field: str,
    value: str,
    *,
    label: str = "Your answer",
    placeholder: str = "",
    min_height: int = 100,
) -> None:
    state["_guided_draft_text"] = value
    state["_guided_draft_field"] = field
    widget = ui.textarea(label, value=value, placeholder=placeholder).classes("w-full")
    widget.props(f"autogrow outlined input-style='min-height: {min_height}px;'")
    widget.on_value_change(lambda event: state.update(_guided_draft_text=event.value or ""))


def _multi_answer(
    state: dict[str, Any],
    field: str,
    choices: list[tuple[str, str]],
    value: tuple[str, ...],
    *,
    label: str,
    placeholder: str,
) -> None:
    state["_guided_draft_multi_field"] = field
    state["_guided_draft_multi"] = list(value)
    widget = _multi_picker(label, choices, allow_custom=True, placeholder=placeholder)
    widget.value = list(value) if choices else ", ".join(value)
    widget.on_value_change(lambda event: state.update(_guided_draft_multi=event.value or []))


def _languages(state: dict[str, Any], field: str, value: tuple[str, ...], label: str) -> None:
    schema: WizardSchema = state["_guided_schema"]
    _multi_answer(
        state,
        field,
        list(language_choices(schema).items()),
        value,
        label=label,
        placeholder="e.g. en, es",
    )


def _source_changed(state: dict[str, Any], source: str, selected: bool) -> None:
    sources: set[str] = state["_guided_draft_sources"]
    if selected:
        sources.add(source)
    else:
        sources.discard(source)


def _commit_current(state: dict[str, Any]) -> GuidedAnswers:
    """Commit only the visible control; preserve answers on every other screen."""
    answers: GuidedAnswers = state["_guided_answers"]
    question = state["_guided_question"]
    updates: dict[str, Any] = {}
    if question == TEMPLATE_QUESTION:
        updates["templates"] = tuple(str(value).strip() for value in state.get("_guided_draft_templates", []) if value)
    elif question == SOURCES_QUESTION:
        sources = tuple(source for source in SOURCES if source in state.get("_guided_draft_sources", set()))
        updates["sources"] = sources
        if SOURCE_METADATA not in sources:
            updates["metadata_properties"] = ()
    elif question in _AUTO_ADVANCE_QUESTIONS:
        field = {
            EXISTING_VALUE_MODE_QUESTION: "existing_value_mode",
            FALLBACK_MODE_QUESTION: "fallback_mode",
            SCOPE_QUESTION: "entity_scope",
            INPUT_LANGUAGE_MODE_QUESTION: "input_language_mode",
            OUTPUT_LANGUAGE_MODE_QUESTION: "output_language_mode",
        }[question]
        updates[field] = state["_guided_radio_value"]
    elif question == GENERATION_QUESTION:
        drafts = state.get("_guided_draft_rules", {})
        if FILL_RULES_QUESTION in drafts:
            updates["fill_rules"] = str(drafts.get(FILL_RULES_QUESTION, "")).strip()
        updates["document_guidance"] = str(drafts.get(DOCUMENT_GUIDANCE_QUESTION, "")).strip()
        updates["additional_rules"] = str(drafts.get(ADDITIONAL_RULES_QUESTION, "")).strip()
        updates["examples"] = str(drafts.get(EXAMPLES_QUESTION, "")).strip()
    elif question in {
        CUSTOM_DESCRIPTION_QUESTION,
        TARGET_PROPERTY_QUESTION,
        EXISTING_VALUE_RULE_QUESTION,
        FALLBACK_VALUE_QUESTION,
        SCOPE_FILTER_QUESTION,
    }:
        field = state.get("_guided_draft_field") or question
        updates[field] = str(state.get("_guided_draft_text", "")).strip()
    elif question in {METADATA_PROPERTIES_QUESTION, INPUT_LANGUAGES_QUESTION, OUTPUT_LANGUAGES_QUESTION}:
        raw = state.get("_guided_draft_multi", [])
        if not isinstance(raw, (list, tuple)):
            raw = str(raw).split(",")
        updates[state["_guided_draft_multi_field"]] = tuple(str(value).strip() for value in raw if str(value).strip())
    return GuidedAnswers(**{**answers.__dict__, **updates})


def _next(state: dict[str, Any]) -> None:
    current = state["_guided_question"]
    if current in _AUTO_ADVANCE_QUESTIONS:
        widget = state.get("_guided_active_radio")
        if widget is not None:
            state["_guided_radio_value"] = widget.value
    answers = _commit_current(state)
    error = question_error(current, answers)
    if error:
        ui.notify(error, type="warning")
        return
    state["_guided_answers"] = answers
    state["_guided_question"] = next_question_id(current, answers)
    _render_question(state)


def _back(state: dict[str, Any]) -> None:
    answers = _commit_current(state)
    current = state["_guided_question"]
    state["_guided_answers"] = answers
    if current == guided_question_ids(answers)[0]:
        state["_guided_back_to_details"]()
        return
    state["_guided_question"] = previous_question_id(current, answers)
    _render_question(state)


def collect_guided_answers(state: dict[str, Any]) -> GuidedAnswers:
    """Return the saved answers plus any edits on the visible screen."""
    if state.get("_guided_answers") is None:
        return GuidedAnswers()
    return _commit_current(state)


def assembled_prompt(state: dict[str, Any]) -> str:
    return assemble_prompt(collect_guided_answers(state), language_label_map(state.get("_guided_schema")))


def _submit(state: dict[str, Any]) -> None:
    answers = collect_guided_answers(state)
    issues = validate_answers(answers)
    if issues:
        ui.notify("Complete the missing answers:\n- " + "\n- ".join(issues), type="warning", multi_line=True)
        return
    prompt = assemble_prompt(answers, language_label_map(state.get("_guided_schema")))
    state["_guided_submit"](prompt, answers)


__all__ = ["assembled_prompt", "build_guided_step", "collect_guided_answers", "ensure_guided_form", "start_guided_flow"]
