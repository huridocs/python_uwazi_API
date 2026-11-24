"""Review-page module split out of the original ``review_page`` (behavior preserved)."""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import quote

from loguru import logger
from nicegui import background_tasks, context, ui

from uwazi_admin_agent.configuration import DUMMY_LANGUAGE
from uwazi_admin_agent.domain.prompt_revision import SuggestionCorrection
from uwazi_admin_agent.domain.select_values import (
    ALL_VALUES_REJECTED_REASON,
    MULTISELECT_TYPE,
    catalog_labels,
    effective_selection,
    unpredicted_values,
    value_options,
)
from uwazi_admin_agent.domain.suggestion import (
    MATCHED_OP,
    MERGE_OPS,
    PROPERTY_OPS,
    SKIPPED_OP,
    OperationReviewRow,
    Suggestion,
    SuggestionStatus,
    skip_reason_label,
)
from uwazi_admin_agent.drivers.web.operation_gate import (
    release,
    release_run,
)
from uwazi_admin_agent.drivers.web.instances import page_link
from uwazi_admin_agent.drivers.web.operations_view import (
    NON_IN_PLACE_OPS,
    file_move_detail_rows,
    is_file_move_row,
    is_merge_anchor,
    is_selectable,
    merge_detail_rows,
    operation_open_url,
    operation_status,
    sub_label,
    sub_revertable,
    sub_status_color,
    sub_status_label,
)
from uwazi_admin_agent.drivers.web.prompt_chat_dialog import prompt_chat_dialog
from uwazi_admin_agent.drivers.web.review_view import (
    MATCH_COLORS,
    RUN_STATUS_COLORS,
    full_value,
    match_label,
    property_caption,
    property_heading,
    revert_entity_confirmation,
    row_neighbours,
)
from uwazi_admin_agent.drivers.web.run_service import (
    entity_is_merge,
    get_entity_basis,
    get_entity_segmentation,
    get_entity_suggestions,
    get_selection_rows,
    get_templates,
    property_labels_for,
    property_types_for,
    resolve_entity_document,
    revert_entity_rows,
    revert_suboperation,
    select_value_catalog,
    set_selected_values,
)
from uwazi_admin_agent.drivers.web.session import (
    SessionCredentials,
    credentials_for_instance,
    page_instance_url,
)

from .core import (
    _claim_gate,
    _clear_checked,
    _current_filter,
    _refresh_all,
    _run_refs,
    _suggestion_is_runnable,
)
from .dialogs import _simple_confirm


def _open_entity_window(
    state: dict[str, Any],
    shared_id: str,
    language: str | None = None,
    focus_property: str | None = None,
) -> None:
    """One entity: its primary document beside all its suggestions.

    AT MOST ONE window is ever open. The row click that opens it is not
    instantaneous (the dialog is built, then its document resolves in a
    background task), so an impatient second click used to leave two or three
    dialogs stacked on top of each other. Any window still open is closed
    before the next one is created, which makes "open one, swap it" the only
    possible outcome no matter how fast the operator clicks.

    The window reads its rows through the review table's CURRENT filter: it is
    the same properties the table shows for that entity and language, narrowed
    the same way, so a filtered table and its detail window cannot disagree.

    ``focus_property`` is set when the window was opened FROM a suboperation:
    the clicked property's card is highlighted.
    """
    _close_entity_window(state)
    suggestions = _entity_rows(state, shared_id, language)
    if not suggestions:
        ui.notify(f"No suggestions for {shared_id}", type="warning")
        return
    title = suggestions[0].entity_title or shared_id
    label = f"{title} — {shared_id}" + (f" · {language}" if language else "")
    state["_entity_home"] = {
        "shared_id": shared_id,
        "language": language,
        "focus_property": focus_property,
        "label": label,
    }
    # Only offer the direction(s) that lead somewhere: no wrap-around, so a
    # single-row result (or the first/last row) never gets a button that just
    # reloads the entity the operator is already reading.
    previous_row, next_row = row_neighbours(_navigation_pairs(state), (shared_id, language))
    with context.client.layout:
        with ui.dialog() as dialog, ui.card().classes("w-full").props("style=height:100dvh"):
            dialog.props("maximized")
            with ui.column().classes("w-full h-full items-stretch no-wrap"):
                # The header and the properties panel are their own hosts: a
                # Run/Revert refreshes them in place (the document viewer on the
                # left keeps its iframe and scroll position).
                header_host = ui.column().classes("w-full shrink-0")
                # The navigation row sits BETWEEN the header and the panels: the
                # operator sees the row controls while looking at the document
                # and the values, without them crowding the top bar.
                if previous_row is not None or next_row is not None:
                    with ui.row().classes("w-full justify-center shrink-0"):
                        if previous_row is not None:
                            ui.button(
                                "Previous row",
                                icon="chevron_left",
                                on_click=lambda: _navigate_entity(state, previous_row),
                            ).props("flat dense")
                        if next_row is not None:
                            ui.button(
                                "Next row",
                                icon="chevron_right",
                                on_click=lambda: _navigate_entity(state, next_row),
                            ).props("flat dense")
                with ui.row().classes("w-full grow no-wrap items-stretch").style("min-height: 0"):
                    document_pane = ui.column().classes("grow").style("min-width: 50%; height: 100%")
                    details_host = ui.column().classes("grow").style("min-width: 45%; height: 100%; overflow-y: auto")
    state["_entity_dialog"] = dialog
    state["_entity_header_host"] = header_host
    state["_entity_details_host"] = details_host
    state["_entity_document_pane"] = document_pane
    _render_entity_header(state, suggestions, label, shared_id, language)
    _render_entity_details(state, suggestions, focus_property)
    dialog.open()
    background_tasks.create(
        _load_document(
            state,
            shared_id,
            language,
            document_pane,
            _basis_properties(suggestions),
            _property_labels(suggestions),
            _property_types_for_suggestions(suggestions),
        ),
        name=f"document {shared_id}",
    )


def _entity_rows(state: dict[str, Any], shared_id: str, language: str | None) -> list[Suggestion]:
    """The entity's rows in one language group, narrowed by the table's filter."""
    return get_entity_suggestions(page_instance_url(), state["run_id"], shared_id, language, _current_filter(state))


def _render_entity_header(
    state: dict[str, Any],
    suggestions: list[Suggestion],
    label: str,
    shared_id: str,
    language: str | None,
) -> None:
    """(Re)draw the window's title row into its host."""
    host = state.get("_entity_header_host")
    if host is None or host.is_deleted:
        return
    host.clear()
    with host:
        _entity_header(
            state,
            label,
            shared_id,
            language,
            _entity_is_discarded(suggestions),
            suggestions,
        )


def _render_entity_details(
    state: dict[str, Any],
    suggestions: list[Suggestion],
    focus_property: str | None,
) -> None:
    """(Re)draw the properties panel into its host."""
    host = state.get("_entity_details_host")
    if host is None or host.is_deleted:
        return
    host.clear()
    with host:
        if not suggestions:
            ui.label("No properties of this entity match the current filter.").classes("text-body2 text-grey-7 q-pa-sm")
            return
        _entity_suggestions(
            state,
            suggestions,
            _property_labels(suggestions),
            focus_property=focus_property,
            types=_property_types_for_suggestions(suggestions),
        )


def _refresh_entity_window(state: dict[str, Any]) -> None:
    """Refresh the window's header + properties after a Run/Revert (in place)."""
    home = state.get("_entity_home")
    dialog = state.get("_entity_dialog")
    if not home or dialog is None or dialog.is_deleted:
        return
    suggestions = _entity_rows(state, home["shared_id"], home["language"])
    _render_entity_header(state, suggestions, home["label"], home["shared_id"], home["language"])
    _render_entity_details(state, suggestions, home["focus_property"])


def _entity_is_discarded(suggestions: list[Suggestion]) -> bool:
    """True when every row of the group was refused (pure)."""
    return bool(suggestions) and all(s.status is SuggestionStatus.DISCARDED for s in suggestions)


def _entity_is_revertable(
    state: dict[str, Any],
    shared_id: str,
    language: str | None,
    suggestions: list[Suggestion],
) -> bool:
    """Whether the entity-level Revert has a write to undo (pure-ish).

    The Revert is entity-wide, not a property action, so it is decided from the
    UNFILTERED language group: a filter that hides the applied rows must not
    hide their undo. The same in-place rule as the table applies — a created
    entity, a deleted one, a create-merge (its target is a dry-created
    placeholder) and a file copy revert at run level, not from this button. An
    ``update_and_delete`` merge IS revertable here: its undo restores the target
    and re-creates the sources it deleted.
    """
    rows = get_entity_suggestions(page_instance_url(), state["run_id"], shared_id, language) or suggestions
    in_place = not any(s.op in NON_IN_PLACE_OPS for s in rows)
    # The table's own rule: an in-place entity with at least one applied row.
    # That covers a whole-entity op (publish/unpublish) whose undo the snapshot
    # restore also performs, not just property writes.
    return in_place and any(s.status is SuggestionStatus.APPLIED for s in rows)


def _close_entity_window(state: dict[str, Any]) -> None:
    """Close the entity window if one is open (the one-window guard)."""
    dialog = state.get("_entity_dialog")
    if dialog is not None and not dialog.is_deleted:
        dialog.close()
    state["_entity_dialog"] = None
    state["_entity_home"] = None
    state["_entity_header_host"] = None
    state["_entity_details_host"] = None
    state["_entity_document_pane"] = None


async def _load_document(
    state: dict[str, Any],
    shared_id: str,
    language: str | None,
    pane: Any,
    basis: list[str] | None = None,
    labels: dict[str, str] | None = None,
    types: dict[str, str] | None = None,
) -> None:
    """Fill the left pane with the entity's PDF viewer, or its document text.

    The decision is made BEFORE rendering: an entity with no uploaded primary
    document gets the segmentation text panel instead of a viewer that would show
    nothing (the plan's no-blank-viewer rule). Both paths degrade to a message
    rather than an empty box.

    ``basis`` short-circuits both: when the rows record that their value was
    inferred from the entity's OWN properties (its title, its ``document_ref``),
    the pane shows those properties' live values — the evidence the operator
    must check — and never resolves or loads a document.

    The document is resolved in the ROW's language with a fallback to the
    instance's other configured languages (an entity usually holds its PDF in
    the language it was uploaded in, not the one being translated), and the
    viewer is pointed at a fully-built server URL — the viewer never has to
    reconstruct the entity's identity from query params.
    """
    if pane.is_deleted:
        return
    with pane:
        ui.linear_progress(show_value=False).classes("w-full")
        ui.label("Loading the entity's document…").classes("text-body2 text-grey-7")
    credentials = credentials_for_instance(page_instance_url())
    if credentials is None:
        _render_pane_message(pane, "Not logged in to Uwazi — cannot show the document.")
        return
    if basis:
        await _render_basis(pane, credentials, shared_id, language, basis, labels or {}, types or {})
        return
    try:
        document = await resolve_entity_document(
            page_instance_url(), credentials.user, credentials.password, shared_id, language
        )
    except Exception as exc:  # noqa: BLE001 — no document info is not an error state
        logger.info("no primary document for {} ({}): {}", shared_id, language, exc)
        document = None
    if pane.is_deleted:  # the operator moved on while we were resolving
        return
    pane.clear()
    if document is not None:
        with pane:
            ui.element("iframe").props(f"src={_viewer_src(state, shared_id, document.language)}").classes("w-full").style(
                "height: 100%; border: 0"
            )
        return
    await _render_segmentation_fallback(pane, credentials, shared_id, language)


def _viewer_src(state: dict[str, Any], shared_id: str, language: str) -> str:
    """The iframe URL that renders an entity's PDF (pure).

    The document route is passed whole through the viewer's ``src`` parameter,
    URL-encoded: an inner ``&`` would otherwise split into the OUTER query
    string and the viewer would fetch an entity-less URL.

    ``v`` busts the browser cache of the viewer's static files (they are served
    with ``max-age=3600``): without it, an operator who already opened the
    window keeps the previous viewer for an hour. Bump it in lockstep with the
    ``?v=`` on the script tag in ``review_viewer.html``.
    """
    document_url = (
        f"/review-pdf?run={quote(str(state['run_id']), safe='')}"
        f"&sid={quote(shared_id, safe='')}"
        f"&lang={quote(language, safe='')}"
    )
    return f"/static/pdfjs/review_viewer.html?src={quote(document_url, safe='')}&v=2"


async def _render_segmentation_fallback(
    pane: Any, credentials: SessionCredentials, shared_id: str, language: str | None = None
) -> None:
    """Show the entity's document TEXT when there is no uploaded primary document."""
    try:
        segmentation = await get_entity_segmentation(
            page_instance_url(),
            credentials.user,
            credentials.password,
            shared_id,
            language or DUMMY_LANGUAGE,
        )
    except Exception as exc:  # noqa: BLE001 — degrade to a message, never a blank pane
        _render_pane_message(pane, f"No primary document, and no readable document text ({exc}).")
        return
    ordered = sorted(segmentation.paragraphs, key=lambda p: (p.page_number, p.top, p.left))
    text = "\n".join(f"[page {p.page_number}] {p.text}" for p in ordered)
    with pane:
        ui.label(f"Document text — {segmentation.filename} (no uploaded primary PDF)").classes("text-subtitle2 text-grey-7")
        ui.code(text or "(no paragraphs)").classes("w-full").style("white-space: pre-wrap; overflow-y: auto; height: 90%")


def _render_pane_message(pane: Any, message: str) -> None:
    if pane.is_deleted:
        return
    pane.clear()
    with pane:
        ui.label(message).classes("text-body2 text-grey-7")


def _basis_properties(suggestions: list[Suggestion]) -> list[str]:
    """The distinct non-empty ``basis`` names across one entity group, in first-appearance order (pure)."""
    names: list[str] = []
    for suggestion in suggestions:
        for name in suggestion.basis:
            if name and name not in names:
                names.append(name)
    return names


async def _render_basis(
    pane: Any,
    credentials: SessionCredentials,
    shared_id: str,
    language: str | None,
    basis: list[str],
    labels: dict[str, str],
    types: dict[str, str],
) -> None:
    """Show the entity's own properties when the recorded value was inferred from them.

    The evidence for such a row is not the PDF: it is the title/``document_ref``
    the script read. Rendering those live values lets the operator check the
    inference without the document viewer implying the value came from a file.
    """
    try:
        raw_language, values = await get_entity_basis(
            page_instance_url(),
            credentials.user,
            credentials.password,
            shared_id,
            language or DUMMY_LANGUAGE,
            basis,
        )
    except Exception as exc:  # noqa: BLE001 — degrade to a message, never a blank pane
        _render_pane_message(pane, f"Could not read the properties this value was inferred from ({exc}).")
        return
    if pane.is_deleted:  # the operator moved on while we were reading
        return
    pane.clear()
    with pane:
        ui.label("Inferred from these properties (no document loaded)").classes("text-subtitle2 text-grey-7")
        ui.label(f"Read from the {raw_language} row").classes("text-caption text-grey-6")
        for name in basis:
            ui.label(property_heading(name, "update", labels)).classes("text-subtitle2")
            caption = property_caption(name, labels)
            if caption:
                ui.label(caption).classes("text-caption text-grey-6")
            _entity_value_label(full_value(values.get(name), types.get(name)), "text-body2")


def _entity_header(
    state: dict[str, Any],
    label: str,
    shared_id: str,
    language: str | None = None,
    discarded: bool = False,
    suggestions: list[Suggestion] | None = None,
) -> None:
    """The window's title row: Run this row, Revert this entity, or Close.

    The buttons mirror the review table's operation actions. Run writes the
    rows that are still waiting (an applied/discarded part is not written
    again) and does so WITHOUT a confirmation dialog — the window is a
    per-entity working surface, so a modal on every press only slowed the loop;
    Revert restores the entity's backup, and is offered only when the entity
    actually has a completed in-place write to undo.
    """
    rows = suggestions or []
    refs = [(s.id, s.shared_id, s.language, s.op) for s in rows if s.id is not None and _suggestion_is_runnable(s)]
    revertable = _entity_is_revertable(state, shared_id, language, rows)
    # The same open-in-Uwazi rule the review table uses: only a row that points
    # at a REAL entity page (not a dry-created placeholder, not a publish-only
    # change) gets the link, so the icon never opens a 404.
    open_url = operation_open_url(
        shared_id,
        [s.op for s in rows],
        [language] if language else [],
        page_instance_url(),
    )
    with ui.row().classes("w-full items-center justify-between no-wrap shrink-0"):
        with ui.row().classes("items-center no-wrap").style("min-width: 0"):
            ui.label(label).classes("text-h6")
            if open_url:
                with (
                    ui.link("", open_url, new_tab=True)
                    .classes("q-ml-sm")
                    .props("rel=noopener")
                    .tooltip("Open this entity in Uwazi")
                ):
                    ui.icon("open_in_new", color="primary").classes("text-subtitle1")
        with ui.row().classes("items-center no-wrap"):
            if discarded:
                ui.badge("discarded").props("color=negative").classes("q-mr-sm")
            run_button = ui.button(
                "Run this row",
                icon="play_arrow",
                on_click=lambda: _run_refs(state, refs, confirm=False),
            ).props("flat dense color=primary")
            if not refs:
                run_button.disable()
            revert_button = ui.button(
                "Revert this entity",
                icon="undo",
                on_click=lambda: _revert_entity_clicked(state, shared_id),
            ).props("flat dense color=indigo")
            if not revertable:
                revert_button.disable()
            ui.button("Close", icon="close", on_click=lambda: _close_entity_window(state)).props("flat dense")


def _template_labels(run_id: str) -> dict[str, dict[str, str]]:
    """``{template_name: {property_name: label}}`` for every template the run writes.

    The review table's child rows and the Property filter show a property's
    human LABEL ("Document Ref"), not the template's raw name
    (``document_ref``). Labels belong to a template, so they are resolved per
    template and cached behind :func:`property_labels_for`; an unreachable
    instance yields ``{}`` and the page falls back to the raw names.
    """
    url = page_instance_url()
    credentials = credentials_for_instance(url)
    if credentials is None:
        return {}
    return {
        name: property_labels_for(url, name, credentials.user, credentials.password) for name in get_templates(url, run_id)
    }


def _merged_labels(by_template: dict[str, dict[str, str]]) -> dict[str, str]:
    """One ``{property_name: label}`` map over every template of a run (pure)."""
    merged: dict[str, str] = {}
    for labels in by_template.values():
        merged.update(labels)
    return merged


def _labels_for_operation(
    item: OperationReviewRow,
    by_template: dict[str, dict[str, str]],
    fallback: dict[str, str],
) -> dict[str, str]:
    """The labels of the templates an operation belongs to (pure).

    An operation can span templates (a merge of entities from two of them), so
    its own template names win over the run-wide fallback: a property name that
    means different things in two templates must read as the label of the
    template the row actually writes.
    """
    if not item.template_names:
        return fallback
    merged: dict[str, str] = {}
    for name in item.template_names:
        merged.update(by_template.get(name) or {})
    return merged or fallback


def _property_labels(suggestions: list[Suggestion]) -> dict[str, str]:
    """The entity's template property labels for the window's headings (cached, best-effort).

    A row names its property by the template's property ``name``
    (``document_ref``); the operator reads it as "Document Ref". The lookup is
    cached per (account, template) behind :func:`property_labels_for`, so it costs
    one ``/api/templates`` read the first time a template's entity is opened and
    nothing after. An unreachable instance yields ``{}`` and the window shows the
    raw names — the same thing it always showed.
    """
    template_name = next((s.template_name for s in suggestions if s.template_name), None)
    if not template_name:
        return {}
    url = page_instance_url()
    credentials = credentials_for_instance(url)
    if credentials is None:
        return {}
    return property_labels_for(url, template_name, credentials.user, credentials.password)


def _template_types(run_id: str) -> dict[str, dict[str, str]]:
    """``{template_name: {property_name: type}}`` for every template the run writes.

    The review table needs a property's TYPE to render its value correctly: a
    ``date``/``daterange``/``multidate`` family value is a numeric timestamp,
    and printing it raw shows an epoch number instead of a calendar date. Types
    belong to a template, so they are resolved per template and cached behind
    :func:`property_types_for`; an unreachable instance yields ``{}`` and the
    generic value rendering is used.
    """
    url = page_instance_url()
    credentials = credentials_for_instance(url)
    if credentials is None:
        return {}
    return {
        name: property_types_for(url, name, credentials.user, credentials.password) for name in get_templates(url, run_id)
    }


def _merged_types(by_template: dict[str, dict[str, str]]) -> dict[str, str]:
    """One ``{property_name: type}`` map over every template of a run (pure)."""
    merged: dict[str, str] = {}
    for types in by_template.values():
        merged.update(types)
    return merged


def _types_for_operation(
    item: OperationReviewRow,
    by_template: dict[str, dict[str, str]],
    fallback: dict[str, str],
) -> dict[str, str]:
    """The property types of the templates an operation belongs to (pure).

    Mirrors :func:`_labels_for_operation`: an operation can span templates, so
    its own template names win over the run-wide fallback, and a property name
    that means different things in two templates is typed by the template the
    row actually writes.
    """
    if not item.template_names:
        return fallback
    merged: dict[str, str] = {}
    for name in item.template_names:
        merged.update(by_template.get(name) or {})
    return merged or fallback


def _property_types_for_suggestions(suggestions: list[Suggestion]) -> dict[str, str]:
    """The entity's template property types for the window's value rendering.

    Cached per (account, template) behind :func:`property_types_for`, so it costs
    one ``/api/templates`` read the first time a template's entity is opened and
    nothing after. An unreachable instance yields ``{}`` and the values render
    through the generic path.
    """
    template_name = next((s.template_name for s in suggestions if s.template_name), None)
    if not template_name:
        return {}
    url = page_instance_url()
    credentials = credentials_for_instance(url)
    if credentials is None:
        return {}
    return property_types_for(url, template_name, credentials.user, credentials.password)


def _entity_value_label(text: str, classes: str) -> None:
    """One value line in the entity window, rendered IN FULL (wrapped, newlines kept).

    The review table truncates to keep cells on one line; the detail window must
    not — a ten-line paragraph has to be readable there. ``pre-wrap`` preserves
    the value's own line breaks and ``overflow-wrap`` stops a long word from
    widening the pane.
    """
    ui.label(text).classes(classes).style("white-space: pre-wrap; overflow-wrap: anywhere; word-break: break-word")


# The rows that show a value the operator can judge as WRONG: a property write,
# a value that matched Uwazi's current one, or an explicit skip with evidence.
# Whole-entity ops and file copies show no proposed property value, so there is
# nothing to attach to a prompt correction.
_CORRECTABLE_OPS: frozenset[str] = PROPERTY_OPS | {MATCHED_OP, SKIPPED_OP}


def _can_fix_prompt(suggestion: Suggestion) -> bool:
    """Whether this row carries a value the operator can flag as wrong (pure)."""
    return suggestion.op in _CORRECTABLE_OPS


def _suggestion_correction(
    suggestion: Suggestion,
    labels: dict[str, str],
    types: dict[str, str],
) -> SuggestionCorrection:
    """The attached-row context for the prompt chat's correction mode (pure).

    The values are rendered HERE, with the template's property type, so a date
    reads as a calendar date and a paragraph keeps its line breaks — exactly the
    text the operator is looking at when they say the value is wrong.
    """
    property_type = types.get(suggestion.property_name or "")
    evidence = (suggestion.context or "").strip()
    if not evidence and suggestion.op == SKIPPED_OP:
        evidence = skip_reason_label(suggestion.skip_reason)
    return SuggestionCorrection(
        shared_id=suggestion.shared_id,
        entity_title=suggestion.entity_title or "",
        template_name=suggestion.template_name,
        language=suggestion.language,
        property_name=suggestion.property_name,
        property_label=property_heading(suggestion.property_name, suggestion.op, labels),
        current_value=full_value(suggestion.current_value, property_type),
        proposed_value=full_value(suggestion.proposed_value, property_type),
        evidence=evidence,
    )


def _fix_prompt_clicked(state: dict[str, Any], correction: SuggestionCorrection) -> None:
    """Open the prompt chat attached to this wrong suggestion (correction mode)."""
    prompt_chat_dialog(
        state["run_id"],
        on_table_changed=lambda: _refresh_after_prompt_fix(state),
        correction=correction,
    )


def _refresh_after_prompt_fix(state: dict[str, Any]) -> None:
    """Repaint the review table AND the open entity window after a fix Run.

    The fix regenerates the script and re-plans the whole table, so this
    window's rows may be obsolete, changed, or brand new: rebuilding it is what
    lets the operator confirm the fixed value without leaving the detail page.
    """
    _refresh_all(state)
    _refresh_entity_window(state)


def _entity_suggestions(
    state: dict[str, Any],
    suggestions: list[Suggestion],
    labels: dict[str, str] | None = None,
    focus_property: str | None = None,
    types: dict[str, str] | None = None,
) -> None:
    """The right-hand panel: the precomputed rows the Run heuristic will write.

    Each card is headed by the property's LABEL — what Uwazi calls it and what
    the operator knows it by — with the raw property name beneath it, so a row is
    both readable and still matchable against the template or a CSV
    (:func:`property_heading` / :func:`property_caption`). ``labels`` is the
    template's label map; without it the heading falls back to the raw name.

    Each card carries the same per-property actions as the review table's
    suboperation row: Run (when the part can still be written on its own) and
    Revert (when that property has a completed in-place write to undo). The
    header's "Run this row"/"Revert this entity" remain the coarse actions.

    ``focus_property`` highlights the property the window was opened from (a
    suboperation's Edit icon) without hiding the entity's other rows.
    ``types`` maps property names to their template type so a date value reads
    as a calendar date rather than a raw epoch timestamp.
    """
    resolved = labels or {}
    property_types = types or {}
    in_place = not any(s.op in NON_IN_PLACE_OPS for s in suggestions)
    merge_rows = [s for s in suggestions if s.op in MERGE_OPS]
    # A merge's PARTS are never revertable on their own: putting one target
    # property back (or undoing one copied file) is not a complete undo of the
    # operation — its row-level Revert restores the target AND re-creates the
    # sources, which is what the table's operation row offers.
    sub_in_place = in_place and not merge_rows
    move_rows = [s for s in suggestions if is_file_move_row(s)]
    for suggestion in suggestions:
        if is_merge_anchor(suggestion) or is_file_move_row(suggestion):
            # The all-no-op merge anchor and a standalone file copy write no
            # property value of their own: the structural cards below show
            # their file move (and, for a merge, its source deletes).
            continue
        classes = "w-full"
        if focus_property is not None and suggestion.property_name == focus_property:
            classes = "w-full bg-blue-1"
        with ui.card().classes(classes):
            with ui.row().classes("w-full items-center justify-between no-wrap"):
                with ui.column().classes("items-start no-wrap").style("min-width: 0"):
                    if suggestion.op == SKIPPED_OP:
                        ui.label("Not processed").classes("text-subtitle2")
                        ui.label(skip_reason_label(suggestion.skip_reason)).classes("text-caption text-grey-7")
                    elif suggestion.op == MATCHED_OP:
                        ui.label(property_heading(suggestion.property_name, suggestion.op, resolved)).classes(
                            "text-subtitle2"
                        )
                    else:
                        ui.label(property_heading(suggestion.property_name, suggestion.op, resolved)).classes(
                            "text-subtitle2"
                        )
                    caption = property_caption(suggestion.property_name, resolved)
                    if caption:
                        ui.label(caption).classes("text-caption text-grey-6")
                with ui.row().classes("items-center no-wrap gap-1"):
                    ui.badge(match_label(suggestion.match)).props(f"color={MATCH_COLORS[match_label(suggestion.match)]}")
                    ui.badge(sub_status_label(suggestion.status)).props(f"color={sub_status_color(suggestion.status)}")
                    if _suggestion_is_runnable(suggestion) and is_selectable(suggestion.op):
                        ui.button(
                            icon="play_arrow",
                            on_click=lambda s=suggestion: _run_refs(
                                state,
                                [(s.id, s.shared_id, s.language, s.op)],
                                [s.op],
                                confirm=False,
                            ),
                        ).props("flat dense color=primary").tooltip("Run this property")
                    if sub_revertable(suggestion, in_place=sub_in_place):
                        ui.button(
                            icon="undo",
                            on_click=lambda s=suggestion: _revert_suboperation_clicked(
                                state,
                                s.shared_id,
                                s.language,
                                s.property_name,
                                s.id,
                                sub_label(s, resolved),
                            ),
                        ).props("flat dense color=indigo").tooltip("Revert this property to its backup value")
            if suggestion.op == MATCHED_OP:
                property_type = property_types.get(suggestion.property_name or "")
                _entity_value_label(
                    f"Current value in Uwazi: {full_value(suggestion.current_value, property_type)}",
                    "text-body2 text-grey-7",
                )
                _entity_value_label(
                    f"Extracted value: {full_value(suggestion.proposed_value, property_type)}",
                    "text-body2 text-teal-9",
                )
            if suggestion.op == SKIPPED_OP and suggestion.proposed_value:
                _entity_value_label(
                    f"Source page / error details: {suggestion.proposed_value}",
                    "text-body2 text-grey-7",
                )
            if suggestion.op in PROPERTY_OPS:
                property_type = property_types.get(suggestion.property_name or "")
                _entity_value_label(
                    f"Value: {full_value(suggestion.current_value, property_type)}", "text-body2 text-grey-7"
                )
                if property_type == MULTISELECT_TYPE:
                    selected = effective_selection(
                        suggestion.proposed_value, suggestion.rejected_values, suggestion.selected_values
                    )
                    available = catalog_labels(_select_catalog_map(suggestion))
                    options = value_options(
                        suggestion.proposed_value,
                        suggestion.rejected_values,
                        selected,
                        available,
                    )
                    # A row discarded BY the value mechanism stays editable
                    # (picking a value back is exactly the review loop); every
                    # other non-runnable state, and an EDITED row (its edit wins
                    # over the values), is read-only.
                    editable = (
                        _suggestion_is_runnable(suggestion) or suggestion.discard_reason == ALL_VALUES_REJECTED_REASON
                    ) and suggestion.status is not SuggestionStatus.EDITED
                    picker = ui.select(
                        {option["label"]: _option_text(option) for option in options},
                        multiple=True,
                        with_input=True,
                        value=selected,
                        label="Values",
                        on_change=lambda e, s=suggestion: _set_selection(
                            state,
                            s,
                            list(e.value) if isinstance(e.value, list) else [],
                        ),
                    ).props("dense outlined")
                    picker.classes("w-full")
                    if not editable:
                        picker.disable()
                    if not available:
                        _entity_value_label(
                            "Could not read this property's thesaurus — only the recommended values are listed.",
                            "text-caption text-grey-7",
                        )
                    unpredicted = unpredicted_values(suggestion.current_value, selected)
                    if unpredicted:
                        _entity_value_label(
                            f"Not predicted, will be removed by this Run: {', '.join(unpredicted)}",
                            "text-body2 text-orange-9",
                        )
                    recommended = [option["label"] for option in options if option["recommended"]]
                    if recommended:
                        _entity_value_label(f"Recommended: {', '.join(recommended)}", "text-caption text-grey-7")
                    _entity_value_label(
                        f"Will write: {', '.join(selected) or '— (nothing)'}",
                        "text-body2 text-teal",
                    )
                else:
                    _entity_value_label(
                        f"Proposed: {full_value(suggestion.proposed_value, property_type)}",
                        "text-body2",
                    )
                if suggestion.context:
                    _entity_value_label(
                        f"Source context: {suggestion.context}",
                        "text-body2 text-grey-7",
                    )
            if suggestion.edited_value is not None:
                _entity_value_label(f"Edited: {suggestion.edited_value}", "text-body2 text-teal")
    if merge_rows:
        status = operation_status([{"status": sub_status_label(row.status)} for row in merge_rows])
        status_color = RUN_STATUS_COLORS.get(status, "grey-7")
        for detail in merge_detail_rows(merge_rows, status=status, status_color=status_color):
            _entity_structural_card(detail)
    if move_rows:
        for detail in file_move_detail_rows(move_rows):
            _entity_structural_card(detail)
    corrections = [_suggestion_correction(s, resolved, property_types) for s in suggestions if _can_fix_prompt(s)]
    if corrections:
        _entity_fix_prompt_button(state, corrections)


def _entity_fix_prompt_button(state: dict[str, Any], corrections: list[SuggestionCorrection]) -> None:
    """The window's ONE "Fix the prompt" action: a grey button floating at the
    bottom right of the screen, above every suggestion card, so the correction
    loop's entry point never scrolls away with the values.

    With a single correctable row it opens the prompt chat directly; with
    several, a small menu lets the operator pick which wrong value to flag.
    """
    with (
        ui.row()
        .classes("fixed bottom-4 right-4 z-40 q-pa-sm q-gutter-x-sm items-center")
        .style("background: rgba(255, 255, 255, 0.9); border-radius: 8px")
    ):
        if len(corrections) == 1:
            ui.button(
                "Fix the prompt",
                icon="edit_note",
                on_click=lambda: _fix_prompt_clicked(state, corrections[0]),
            ).props("unelevated color=grey-8 size=md").tooltip(
                "Explain why this value is wrong and regenerate the script from the correction"
            )
        else:
            with ui.dropdown_button(
                "Fix the prompt",
                icon="edit_note",
                auto_close=True,
            ).props("unelevated color=grey-8 size=md"):
                for correction in corrections:
                    ui.item(
                        correction.property_label or correction.property_name or "(row)",
                        on_click=lambda c=correction: _fix_prompt_clicked(state, c),
                    ).props("clickable").tooltip("Flag this value as wrong")


def _entity_structural_card(detail: dict[str, Any]) -> None:
    """One display-only file/delete card in the entity window (pure payload in)."""
    with ui.card().classes("w-full"):
        with ui.row().classes("w-full items-center justify-between no-wrap"):
            with ui.column().classes("items-start no-wrap").style("min-width: 0"):
                ui.label(detail["label"]).classes("text-subtitle2")
                if detail["caption"]:
                    ui.label(detail["caption"]).classes("text-caption text-grey-6")
            with ui.row().classes("items-center no-wrap gap-1"):
                ui.badge(detail["match"]).props(f"color={detail['match_color']}")
                ui.badge(detail["status"]).props(f"color={detail['status_color']}")
        _entity_value_label(f"Value: {detail['current']}", "text-body2 text-grey-7")
        _entity_value_label(f"Proposed: {detail['proposed']}", "text-body2")


def _navigation_pairs(state: dict[str, Any]) -> list[tuple[str, str | None]]:
    """The (sharedId, language) rows the window can walk, in the filter's order.

    Navigation walks (sharedId, language) pairs, so it lands on the next row of
    the table the operator is looking at — for a translation task that means the
    sibling language of the same entity, not a different entity. The pairs come
    from the store's selection projection (the filter is a suboperation filter,
    so several parts of one group collapse to one pair).
    """
    pairs: list[tuple[str, str | None]] = []
    for _id, shared_id, row_language, _op in get_selection_rows(
        page_instance_url(), state["run_id"], _current_filter(state)
    ):
        pair = (shared_id, row_language)
        if pair not in pairs:
            pairs.append(pair)
    return pairs


def _navigate_entity(state: dict[str, Any], target: tuple[str, str | None]) -> None:
    """Open another row of the current filter's list (the window's Previous/Next).

    The button is only created when :func:`row_neighbours` found that row, so
    this never wraps around to the current entity.
    """
    _open_entity_window(state, target[0], target[1])


async def _revert_entity_clicked(state: dict[str, Any], shared_id: str) -> None:
    """Revert one entity (all its languages) to its single backup, then refresh.

    Confirm first: the restore overwrites the entity's rows in every language
    the task touched, not just the clicked suboperation's language. A merge row
    also re-creates the sources it absorbed, so the dialog says so.
    """
    tooltip = state.get("_backup_dates", {}).get(shared_id)
    tooltip_text = tooltip.strftime("%Y-%m-%d %H:%M") if tooltip is not None else ""

    def _confirm() -> None:
        client = context.client
        background_tasks.create(_revert_entity_rows(state, shared_id, client), name=f"revert {shared_id}")

    title, message = revert_entity_confirmation(
        shared_id,
        tooltip_text,
        merge=entity_is_merge(page_instance_url(), state["run_id"], shared_id),
    )
    _simple_confirm(title, message, _confirm)


def _revert_suboperation_clicked(
    state: dict[str, Any],
    shared_id: str,
    language: str | None,
    property_name: str,
    suggestion_id: int,
    label: str,
) -> None:
    """Undo ONE property write (the suboperation's own Revert).

    The entity-wide Revert would throw away every other write to the entity;
    this one puts a single property back, so the operator can keep the rest.
    Confirm first — it still writes to Uwazi.
    """

    def _confirm() -> None:
        client = context.client
        background_tasks.create(
            _revert_suboperation(
                state,
                shared_id,
                language,
                property_name,
                suggestion_id,
                client,
            ),
            name=f"revert sub {suggestion_id}",
        )

    title = f"Revert {label}?"
    message = (
        f"Put this one property back to the value the run's backup captured for {shared_id}"
        + (f" ({language})" if language else "")
        + ". Every other property, and every other language, keeps the run's write."
    )
    _simple_confirm(title, message, _confirm)


async def _revert_suboperation(
    state: dict[str, Any],
    shared_id: str,
    language: str | None,
    property_name: str,
    suggestion_id: int,
    client: Any,
) -> None:
    """Execute one suboperation revert in a worker thread, then refresh."""
    credentials = credentials_for_instance(state["instance_url"])
    if credentials is None:
        with client:
            ui.notify("Not logged in to Uwazi", type="negative")
        return
    # One Uwazi-write operation at a time per instance (the gate), as for the Run.
    if not _claim_gate(state, "revert", client):
        return
    try:
        try:
            row_language = await revert_suboperation(
                state["instance_url"],
                state["run_id"],
                shared_id,
                language,
                property_name,
                suggestion_id,
                credentials.user,
                credentials.password,
            )
            with client:
                ui.notify(
                    f"Reverted {property_name} ({row_language}) to its backup value",
                    type="positive",
                )
        except Exception as exc:  # noqa: BLE001 — the toast reports; the page never crashes
            logger.exception("suboperation revert failed")
            with client:
                ui.notify(
                    f"Revert failed: {exc}",
                    type="negative",
                    multi_line=True,
                )
    finally:
        release(state["instance_url"], "revert", state["run_id"])
        release_run(state["instance_url"], state["run_id"], "revert")
        with client:
            _clear_checked(state)
            _refresh_all(state)
            _refresh_entity_window(state)


async def _revert_entity_rows(state: dict[str, Any], shared_id: str, client: Any) -> None:
    credentials = credentials_for_instance(state["instance_url"])
    if credentials is None:
        with client:
            ui.notify("Not logged in to Uwazi", type="negative")
        return
    # One Uwazi-write operation at a time per instance (the gate), as for the Run.
    if not _claim_gate(state, "revert", client):
        return
    state["_progress"](0, 1)
    try:
        try:
            info = await revert_entity_rows(
                state["instance_url"], state["run_id"], shared_id, credentials.user, credentials.password
            )
            state["_progress"](1, 1)
            with client:
                ui.notify(
                    f"Reverted {shared_id} to its backup"
                    + (f" ({info.backup_created_at:%Y-%m-%d %H:%M})" if info.backup_created_at else ""),
                    type="positive",
                )
        except Exception as exc:  # noqa: BLE001 — the toast reports, the page never crashes
            logger.exception("per-entity revert failed")
            with client:
                ui.notify(
                    f"Revert failed: {exc}",
                    type="negative",
                    multi_line=True,
                )
    finally:
        release(state["instance_url"], "revert", state["run_id"])
        release_run(state["instance_url"], state["run_id"], "revert")
        with client:
            _clear_checked(state)
            _refresh_all(state)
            _refresh_entity_window(state)


def _option_text(option: dict[str, Any]) -> str:
    """The picker's DISPLAY text for one option (pure): the raw label, marked when recommended.

    The select's option dict is keyed by the raw thesaurus label and this string
    is only what the widget renders, so the element's ``.value`` — and therefore
    every persisted selection — never carries the suffix.
    """
    return f"{option['label']} (recommended)" if option["recommended"] else str(option["label"])


def _select_catalog_map(suggestion: Suggestion) -> dict[str, str]:
    """``{label: value id}`` of the row's thesaurus (best-effort; ``{}`` when unreachable)."""
    if not suggestion.template_name or not suggestion.property_name:
        return {}
    url = page_instance_url()
    credentials = credentials_for_instance(url)
    if credentials is None:
        return {}
    return select_value_catalog(
        url,
        suggestion.template_name,
        suggestion.property_name,
        credentials.user,
        credentials.password,
    )


def _set_selection(state: dict[str, Any], suggestion: Suggestion, selected: list[str] | None) -> None:
    """Persist the picker's selection for one row (the select's ``on_change`` handler)."""
    if suggestion.id is None:
        return
    background_tasks.create(
        _set_selection_by_id(state, suggestion.id, selected, context.client),
        name=f"select values {suggestion.id}",
    )


async def _set_selection_by_id(state: dict[str, Any], row_id: int, selected: list[str] | None, client: Any) -> None:
    """Persist one multiselect row's explicit selection, then refresh page + window.

    ``None`` drops the explicit selection (the proposal applies again); ``[]``
    is the operator's "nothing selected", which the service turns into the
    row's sentinel discard.
    """
    try:
        await asyncio.to_thread(
            set_selected_values,
            page_instance_url(),
            state["run_id"],
            row_id,
            selected,
        )
    except Exception as exc:  # noqa: BLE001 — the toast reports; the page never crashes
        logger.exception("multiselect value update failed")
        with client:
            ui.notify(f"Could not update the value selection: {exc}", type="negative", multi_line=True)
        return
    with client:
        _refresh_entity_window(state)
        _refresh_all(state)
