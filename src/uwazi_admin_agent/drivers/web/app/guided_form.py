"""The guided question form of the New Task wizard.

The wizard offers two ways to produce a prompt: write it directly, or answer a
fixed set of questions that is assembled heuristically into the prompt
(:mod:`uwazi_admin_agent.domain.guided_task`). This module is the second way's UI
only — it collects answers, keeps the dynamic branches consistent with them,
shows a live preview of the prompt it will build, and hands that prompt back to
the wizard, which then follows the existing validate → generate flow.

The questions are built from the instance's real templates/properties and
configured languages, read once per account by
:func:`uwazi_admin_agent.drivers.web.run_service.wizard_schema.load_wizard_schema`.
When that read fails the step says so, offers a retry, and can send the operator
back to free-text entry — it never pretends the instance has no templates.

No LLM is involved anywhere in this module.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from loguru import logger
from nicegui import background_tasks, ui

from uwazi_admin_agent.domain.guided_task import (
    EXISTING_CUSTOM,
    EXISTING_REPLACE,
    EXISTING_VALUE_LABELS,
    FALLBACK_BLANK,
    FALLBACK_LABELS,
    FALLBACK_VALUE,
    GOAL_CLASSIFY,
    GOAL_DESCRIPTIONS,
    GOAL_FILL,
    GOALS,
    INPUT_LANGUAGE_LABELS,
    INPUT_LANGUAGE_MATCH,
    INPUT_LANGUAGE_MODES,
    INPUT_LANGUAGE_SPECIFIC,
    OUTPUT_ALL,
    OUTPUT_LANGUAGE_LABELS,
    OUTPUT_LANGUAGE_MODES,
    OUTPUT_SPECIFIC,
    SCOPE_ALL,
    SCOPE_FILTER,
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


def build_guided_step(
    state: dict[str, Any],
    dialog: Any,
    *,
    on_submit: Callable[[str, GuidedAnswers], None],
    on_back: Callable[[], None],
    on_free_text: Callable[[], None],
) -> None:
    """Create the wizard's ``guided`` step; its form loads lazily on first use."""
    with ui.step(name="guided", title="Questions", icon="quiz"):
        state["_guided_submit"] = on_submit
        state["_guided_free_text"] = on_free_text
        state["_guided_dialog"] = dialog
        state["_guided_container"] = ui.column().classes("w-full")
        with ui.row().classes("q-mt-lg w-full justify-end"):
            ui.button("Cancel", on_click=lambda: dialog.close()).props("color=grey-7 flat")
            ui.button("Back", on_click=on_back).props("color=grey-7 flat")
            submit = ui.button("Build prompt", icon="arrow_forward", on_click=lambda: _submit(state))
            submit.props("color=primary")
            state["_guided_submit_button"] = submit


def ensure_guided_form(state: dict[str, Any], *, refresh: bool = False) -> None:
    """Load the instance schema (once) and render the questions into the step.

    Callable repeatedly and from a stepper change handler: an in-flight or
    finished load is never restarted, so returning to the step keeps the answers
    the operator already gave. ``refresh=True`` re-reads the instance schema
    (the error panel's Retry), because templates may have changed since the
    cached read.
    """
    if state.get("_guided_loading") or (state.get("_guided_built") and not refresh):
        return
    if refresh:
        state.pop("_guided_built", None)
    container = state.get("_guided_container")
    if container is None:
        return
    state["_guided_loading"] = True
    _set_submit_enabled(state, False)
    _render_loading(container)
    background_tasks.create(_load_and_render(state, refresh=refresh), name="guided task schema")


def _render_loading(container: Any) -> None:
    container.clear()
    with container, ui.row().classes("w-full items-center gap-2"):
        ui.spinner(size="lg")
        ui.label("Reading the instance's templates and languages...").classes("text-body1")


async def _load_and_render(state: dict[str, Any], *, refresh: bool = False) -> None:
    """Fetch the schema off the event loop, then build the questions (or degrade)."""
    container = state["_guided_container"]
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
    except Exception as exc:  # noqa: BLE001 — the step degrades, never blocks free-text entry
        logger.warning("could not read the instance schema for the guided wizard: {}", exc)
        _render_schema_error(state, exc)
        return
    state["_guided_loading"] = False
    state["_guided_schema"] = schema
    _render_form(state, container, schema)


def _render_schema_error(state: dict[str, Any], exc: Exception) -> None:
    container = state["_guided_container"]
    state["_guided_loading"] = False
    container.clear()
    with container:
        ui.label("Could not read the instance's templates").classes("text-h6 text-negative")
        ui.label(str(exc)).classes("text-body2 q-mb-sm")
        ui.label(
            "The questions need the template list to offer real property names. Retry, or describe "
            "the task in your own words instead."
        ).classes("text-body2 text-grey-7")
        _set_submit_enabled(state, False)
        with ui.row().classes("q-mt-md w-full justify-end"):
            ui.button("Use free-text prompt", on_click=lambda: state["_guided_free_text"]()).props("color=grey-7 flat")
            ui.button("Retry", icon="refresh", on_click=lambda: _retry(state)).props("color=primary")


def _set_submit_enabled(state: dict[str, Any], enabled: bool) -> None:
    """Show/hide the step's Build prompt button (it is meaningless without a form)."""
    button = state.get("_guided_submit_button")
    if button is not None:
        button.set_visibility(enabled)


def _retry(state: dict[str, Any]) -> None:
    state["_guided_loading"] = False
    ensure_guided_form(state, refresh=True)


# --- Form construction ----------------------------------------------------


def _multi_picker(
    label: str,
    choices: list[tuple[str, str]],
    *,
    allow_custom: bool,
    placeholder: str,
) -> Any:
    """A multi-select over ``choices``, or a comma-separated input when empty."""
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
    """A single select over ``choices``, or a text input when none are known.

    Custom values are allowed: a prompt may name a property this instance does
    not define yet (or the template selection is broader than the offered set),
    and the operator must be able to type the slug instead of being blocked by
    the schema's current contents.
    """
    if choices:
        return (
            ui.select(dict(choices), label=label, with_input=True, clearable=True, new_value_mode="add-unique")
            .props("outlined")
            .classes("w-full")
        )
    return ui.input(label, placeholder=placeholder).classes("w-full")


def _radio(options: dict[str, str], value: str) -> Any:
    return ui.radio(options, value=value).props("dense").classes("w-full")


def _render_form(state: dict[str, Any], container: Any, schema: WizardSchema) -> None:
    widgets: dict[str, Any] = {}
    state["_guided_widgets"] = widgets
    container.clear()
    with container:
        # ONE scroll region holds the questions AND the prompt preview, and it
        # has a fixed height: an autogrowing preview made the dialog taller than
        # the viewport as the prompt grew, pushing the step's buttons out of
        # reach (Quasar does not scroll a dialog card that overflows).
        with ui.scroll_area().classes("w-full").style("height: 50vh"):
            with ui.column().classes("w-full gap-1"):
                _render_goal_section(widgets, schema)
                _render_input_section(widgets, schema)
                state["_guided_branch_container"] = ui.column().classes("w-full")
                _render_branch(state)
                _render_output_section(widgets, schema)
                ui.label("Prompt preview").classes("text-subtitle1 text-grey-7 q-mt-md")
                state["_guided_preview"] = (
                    ui.textarea().classes("w-full font-mono").props("readonly outlined").style("height: 240px")
                )
        state["_guided_issues"] = ui.label().classes("text-body2 text-orange-9")

    # Marked built ONLY after the form exists: an exception while building must
    # leave the step retryable instead of showing an empty panel forever.
    state["_guided_built"] = True
    _set_submit_enabled(state, True)
    _wire(state)
    _refresh_preview(state)


def _render_goal_section(widgets: dict[str, Any], schema: WizardSchema) -> None:
    ui.label("Step 1 — Goal & target entities").classes("text-subtitle1 text-bold q-mt-sm")
    widgets["goal"] = _radio({goal: GOAL_DESCRIPTIONS[goal] for goal in GOALS}, GOAL_CLASSIFY)
    widgets["templates"] = (
        ui.select(
            [template.name for template in schema.templates],
            label="Template(s) to analyze",
            multiple=True,
            with_input=True,
            clearable=True,
            new_value_mode="add-unique",
        )
        .props("outlined use-chips")
        .classes("w-full q-mt-sm")
    )
    ui.label("Leave empty for all templates. A name typed here is used as written.").classes(
        "text-body2 text-grey-7"
    )
    widgets["scope"] = _radio({scope: SCOPE_LABELS[scope] for scope in SCOPES}, SCOPE_ALL)
    widgets["scope_filter"] = ui.input(
        "Filter or condition",
        placeholder="e.g. creationDate > 2025-01-01, document_status = Adopted",
    ).classes("w-full")
    widgets["scope_filter"].set_visibility(False)


def _render_input_section(widgets: dict[str, Any], schema: WizardSchema) -> None:
    ui.label("Step 2 — Input sources & languages").classes("text-subtitle1 text-bold q-mt-lg")
    ui.label("Which information should be used as input?").classes("text-body1 q-mt-sm")
    widgets["sources"] = {source: ui.checkbox(SOURCE_LABELS[source]).classes("w-full") for source in SOURCES}
    widgets["metadata_properties"] = _multi_picker(
        "Metadata properties to read",
        metadata_property_choices(schema, ()),
        allow_custom=True,
        placeholder="e.g. title, summary, description",
    )
    widgets["metadata_properties"].set_visibility(False)
    widgets["document_guidance"] = (
        ui.textarea(
            "Where does the relevant information live? (optional)",
            placeholder=(
                "e.g. Look at the first 2 pages for the header, date and author. "
                "Scan the 'Conclusions' or 'Ruling' section near the end."
            ),
        )
        .classes("w-full q-mt-sm")
        .props("autogrow")
    )
    ui.label("Which input language should be read when an entity has multiple versions?").classes(
        "text-body1 q-mt-sm"
    )
    widgets["input_language_mode"] = _radio(
        {mode: INPUT_LANGUAGE_LABELS[mode] for mode in INPUT_LANGUAGE_MODES},
        INPUT_LANGUAGE_MATCH,
    )
    widgets["input_languages"] = _multi_picker(
        "Primary input language(s)",
        list(language_choices(schema).items()),
        allow_custom=not schema.languages,
        placeholder="e.g. en, es",
    )
    widgets["input_languages"].set_visibility(False)


def _render_output_section(widgets: dict[str, Any], schema: WizardSchema) -> None:
    ui.label("Step 4 — Output languages & instructions").classes("text-subtitle1 text-bold q-mt-lg")
    ui.label("How should multilingual outputs be generated?").classes("text-body1 q-mt-sm")
    widgets["output_language_mode"] = _radio(
        {mode: OUTPUT_LANGUAGE_LABELS[mode] for mode in OUTPUT_LANGUAGE_MODES},
        OUTPUT_ALL,
    )
    widgets["output_languages"] = _multi_picker(
        "Output language(s)",
        list(language_choices(schema).items()),
        allow_custom=not schema.languages,
        placeholder="e.g. en, es",
    )
    widgets["output_languages"].set_visibility(False)
    ui.label(
        "Text properties get one row per language; shared properties (dates, selects, relationships) "
        "get a single unified suggestion."
    ).classes("text-body2 text-grey-7")
    widgets["additional_rules"] = (
        ui.textarea(
            "Additional instructions or edge cases (optional)",
            placeholder="e.g. Keep legal terminology formal. Ignore text inside footnotes and headers.",
        )
        .classes("w-full q-mt-sm")
        .props("autogrow")
    )
    widgets["examples"] = (
        ui.textarea(
            "Examples of expected results (optional)",
            placeholder="e.g. 'Plaintiff was granted leave to appeal' -> 'Leave to appeal: granted'",
        )
        .classes("w-full")
        .props("autogrow")
    )


def _wire(state: dict[str, Any]) -> None:
    """Connect the controls to the reactions they need."""
    widgets = state["_guided_widgets"]
    widgets["goal"].on_value_change(lambda: _render_branch(state))
    widgets["templates"].on_value_change(lambda: _templates_changed(state))
    widgets["scope"].on_value_change(lambda: _scope_changed(state))
    for checkbox in widgets["sources"].values():
        checkbox.on_value_change(lambda: _sources_changed(state))
    widgets["input_language_mode"].on_value_change(lambda: _input_language_changed(state))
    widgets["output_language_mode"].on_value_change(lambda: _output_language_changed(state))
    preview_only = (
        "metadata_properties",
        "document_guidance",
        "input_languages",
        "output_languages",
        "additional_rules",
        "examples",
    )
    for key in preview_only:
        widgets[key].on_value_change(lambda: _refresh_preview(state))


def _sources_changed(state: dict[str, Any]) -> None:
    widgets = state["_guided_widgets"]
    widgets["metadata_properties"].set_visibility(bool(widgets["sources"][SOURCE_METADATA].value))
    _refresh_preview(state)


def _scope_changed(state: dict[str, Any]) -> None:
    widgets = state["_guided_widgets"]
    widgets["scope_filter"].set_visibility(widgets["scope"].value == SCOPE_FILTER)
    _refresh_preview(state)


def _input_language_changed(state: dict[str, Any]) -> None:
    widgets = state["_guided_widgets"]
    widgets["input_languages"].set_visibility(widgets["input_language_mode"].value == INPUT_LANGUAGE_SPECIFIC)
    _refresh_preview(state)


def _output_language_changed(state: dict[str, Any]) -> None:
    widgets = state["_guided_widgets"]
    widgets["output_languages"].set_visibility(widgets["output_language_mode"].value == OUTPUT_SPECIFIC)
    _refresh_preview(state)


def _templates_changed(state: dict[str, Any]) -> None:
    """Re-offer the property pickers for the newly selected templates."""
    widgets = state["_guided_widgets"]
    templates = _multi_value(widgets["templates"])
    _replace_options(
        widgets["metadata_properties"],
        metadata_property_choices(state["_guided_schema"], templates),
    )
    _render_branch(state)


def _replace_options(widget: Any, choices: list[tuple[str, str]]) -> None:
    """Swap a select's options without dropping an explicit selection.

    A value the operator typed (``new_value_mode``) or picked under a wider
    template selection is KEPT as an option: narrowing the template list must
    not silently erase an answer that will still appear in the prompt.
    """
    if not isinstance(widget, ui.select):
        return
    options = dict(choices)
    if widget.multiple:
        current = list(_multi_value(widget))
        for value in current:
            options.setdefault(value, value)
    else:
        current = widget.value
        if current:
            options.setdefault(current, current)
    widget.set_options(options, value=current)


# --- The dynamic Step 3 branch -------------------------------------------


def _capture_branch(state: dict[str, Any]) -> None:
    """Remember the branch answers before the branch is rebuilt (pure read)."""
    previous_goal = state.get("_guided_branch_goal")
    branch = state.get("_guided_branch") or {}
    if previous_goal is None or not branch:
        return
    state.setdefault("_guided_branch_values", {})[previous_goal] = {
        "target_property": _text(branch.get("target_property")),
        "fallback_mode": _value(branch.get("fallback_mode"), FALLBACK_BLANK),
        "fallback_value": _text(branch.get("fallback_value")),
        "fill_rules": _text(branch.get("fill_rules")),
        "existing_value_mode": _value(branch.get("existing_value_mode"), EXISTING_REPLACE),
        "existing_value_rule": _text(branch.get("existing_value_rule")),
        "custom_description": _text(branch.get("custom_description")),
    }


def _restore_branch(state: dict[str, Any], goal: str, branch: dict[str, Any]) -> None:
    """Re-apply the answers this goal had before the branch was rebuilt."""
    saved = (state.get("_guided_branch_values") or {}).get(goal) or {}
    if not saved:
        return
    for key, value in saved.items():
        widget = branch.get(key)
        if widget is None or value in ("", None):
            continue
        if isinstance(widget, ui.select) and not widget.multiple and value not in widget.options:
            # A property chosen before the template selection narrowed: keep
            # the answer visible rather than silently erasing it.
            options = dict(widget.options)
            options[value] = value
            widget.set_options(options)
        widget.value = value
    # The reveal states follow the restored answers, not the widget defaults.
    if branch.get("fallback_mode") is not None:
        branch["fallback_value"].set_visibility(branch["fallback_mode"].value == FALLBACK_VALUE)
    if branch.get("existing_value_mode") is not None:
        branch["existing_value_rule"].set_visibility(branch["existing_value_mode"].value == EXISTING_CUSTOM)


def _render_branch(state: dict[str, Any]) -> None:
    """Rebuild Step 3 for the selected goal, keeping answers already given."""
    _capture_branch(state)
    container = state["_guided_branch_container"]
    widgets = state["_guided_widgets"]
    schema: WizardSchema = state["_guided_schema"]
    goal = _value(widgets["goal"], GOAL_CLASSIFY)
    templates = _multi_value(widgets["templates"])
    branch: dict[str, Any] = {}
    state["_guided_branch"] = branch
    state["_guided_branch_goal"] = goal

    container.clear()
    with container:
        ui.label("Step 3 — Task details").classes("text-subtitle1 text-bold q-mt-lg")
        if goal == GOAL_CLASSIFY:
            ui.label(
                "Select, multi-select and relationship properties accept thesaurus terms or linked entities."
            ).classes("text-body2 text-grey-7")
            branch["target_property"] = _single_picker(
                "Target property for the suggested topics",
                property_choices(schema, templates, GOAL_CLASSIFY),
                placeholder="e.g. topics",
            )
            branch["fallback_mode"] = _radio(
                {mode: FALLBACK_LABELS[mode] for mode in (FALLBACK_BLANK, FALLBACK_VALUE)},
                FALLBACK_BLANK,
            )
            branch["fallback_value"] = ui.input("Fallback value", placeholder="e.g. Uncategorized").classes(
                "w-full"
            )
            branch["fallback_value"].set_visibility(False)
            branch["fallback_mode"].on_value_change(
                lambda: branch["fallback_value"].set_visibility(branch["fallback_mode"].value == FALLBACK_VALUE)
            )
        elif goal == GOAL_FILL:
            branch["target_property"] = _single_picker(
                "Target property for the suggested value",
                property_choices(schema, templates, GOAL_FILL),
                placeholder="e.g. incident_date, judge_name, executive_summary",
            )
            branch["fill_rules"] = (
                ui.textarea(
                    "Rules or formatting for the generated value",
                    placeholder=(
                        "e.g. Summarize the ruling in 3 concise bullet points. Extract the date the "
                        "document was signed, ignoring upload or stamp dates."
                    ),
                )
                .classes("w-full q-mt-sm")
                .props("autogrow")
            )
            ui.label("How should suggestions behave if the property already has a value?").classes(
                "text-body1 q-mt-sm"
            )
            branch["existing_value_mode"] = _radio(EXISTING_VALUE_LABELS, EXISTING_REPLACE)
            branch["existing_value_rule"] = ui.input(
                "Custom rule",
                placeholder="e.g. Only replace when the existing text is under 20 words",
            ).classes("w-full")
            branch["existing_value_rule"].set_visibility(False)
            branch["existing_value_mode"].on_value_change(
                lambda: branch["existing_value_rule"].set_visibility(
                    branch["existing_value_mode"].value == EXISTING_CUSTOM
                )
            )
        else:
            branch["custom_description"] = (
                ui.textarea(
                    "Describe what to analyze, extract or suggest",
                    placeholder=(
                        "Explain which fields or documents to inspect, what logic or criteria to apply, "
                        "and what suggestions should appear in the review table."
                    ),
                )
                .classes("w-full")
                .props("autogrow input-style='min-height: 160px;'")
            )
        _restore_branch(state, goal, branch)
        for widget in branch.values():
            widget.on_value_change(lambda: _refresh_preview(state))
    _refresh_preview(state)


# --- Answers, preview and submit -----------------------------------------


def _text(widget: Any) -> str:
    if widget is None:
        return ""
    value = getattr(widget, "value", None)
    return str(value).strip() if value is not None else ""


def _multi_value(widget: Any) -> tuple[str, ...]:
    """Read a multi-select or a comma-separated input as stripped values."""
    if widget is None:
        return ()
    value = getattr(widget, "value", None)
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return tuple(part.strip() for part in str(value).split(",") if part.strip())


def _value(widget: Any, default: str) -> str:
    return str(widget.value) if widget is not None and widget.value else default


def collect_guided_answers(state: dict[str, Any]) -> GuidedAnswers:
    """Read the current form values into a :class:`GuidedAnswers`."""
    widgets = state.get("_guided_widgets") or {}
    branch = state.get("_guided_branch") or {}
    sources = widgets.get("sources") or {}
    return GuidedAnswers(
        goal=_value(widgets.get("goal"), GOAL_CLASSIFY),
        templates=_multi_value(widgets.get("templates")),
        entity_scope=_value(widgets.get("scope"), SCOPE_ALL),
        scope_filter=_text(widgets.get("scope_filter")),
        sources=tuple(source for source in SOURCES if sources.get(source) is not None and bool(sources[source].value)),
        metadata_properties=_multi_value(widgets.get("metadata_properties")),
        document_guidance=_text(widgets.get("document_guidance")),
        input_language_mode=_value(widgets.get("input_language_mode"), INPUT_LANGUAGE_MATCH),
        input_languages=_multi_value(widgets.get("input_languages")),
        target_property=_text(branch.get("target_property")),
        fallback_mode=_value(branch.get("fallback_mode"), FALLBACK_BLANK),
        fallback_value=_text(branch.get("fallback_value")),
        fill_rules=_text(branch.get("fill_rules")),
        existing_value_mode=_value(branch.get("existing_value_mode"), EXISTING_REPLACE),
        existing_value_rule=_text(branch.get("existing_value_rule")),
        custom_description=_text(branch.get("custom_description")),
        output_language_mode=_value(widgets.get("output_language_mode"), OUTPUT_ALL),
        output_languages=_multi_value(widgets.get("output_languages")),
        additional_rules=_text(widgets.get("additional_rules")),
        examples=_text(widgets.get("examples")),
    )


def assembled_prompt(state: dict[str, Any]) -> str:
    """The prompt the current answers produce (pure over the form state)."""
    return assemble_prompt(collect_guided_answers(state), language_label_map(state.get("_guided_schema")))


def _refresh_preview(state: dict[str, Any]) -> None:
    if not state.get("_guided_built"):
        return
    preview = state.get("_guided_preview")
    if preview is not None:
        preview.value = assembled_prompt(state)
    issues = validate_answers(collect_guided_answers(state))
    label = state.get("_guided_issues")
    if label is not None:
        label.set_text("; ".join(issues) if issues else "")


def _submit(state: dict[str, Any]) -> None:
    """Validate the answers, assemble the prompt and hand it to the wizard."""
    answers = collect_guided_answers(state)
    issues = validate_answers(answers)
    if issues:
        ui.notify("Complete the missing answers:\n- " + "\n- ".join(issues), type="warning", multi_line=True)
        return
    prompt = assemble_prompt(answers, language_label_map(state.get("_guided_schema")))
    state["_guided_submit"](prompt, answers)


__all__ = ["assembled_prompt", "build_guided_step", "collect_guided_answers", "ensure_guided_form"]
