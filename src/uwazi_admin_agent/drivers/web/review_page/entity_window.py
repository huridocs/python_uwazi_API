"""Review-page module split out of the original ``review_page`` (behavior preserved)."""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import quote

from loguru import logger
from nicegui import background_tasks, context, ui

from uwazi_admin_agent.configuration import DUMMY_LANGUAGE
from uwazi_admin_agent.domain.prompt_revision import SuggestionCorrection
from uwazi_admin_agent.domain.relationship_values import (
    RELATIONSHIP_TYPE,
    SELECT_TYPE,
    relationship_selection,
    render_relationship,
)
from uwazi_admin_agent.domain.select_values import (
    ALL_VALUES_REJECTED_REASON,
    MULTISELECT_TYPE,
    catalog_labels,
    effective_selection,
    unpredicted_values,
    value_options,
)
from uwazi_admin_agent.domain.suggestion import (
    CREATE_OPS,
    MATCHED_OP,
    MERGE_OPS,
    PROPERTY_OPS,
    SKIPPED_OP,
    OperationReviewRow,
    Suggestion,
    SuggestionStatus,
    skip_reason_label,
)
from uwazi_admin_agent.drivers.web.instances import page_link
from uwazi_admin_agent.drivers.web.operation_gate import (
    release,
    release_run,
)
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
    property_heading,
    revert_entity_confirmation,
    row_neighbours,
)
from uwazi_admin_agent.drivers.web.run_service import (
    add_validation_sample,
    entity_is_merge,
    entity_is_validation,
    get_entity_basis,
    get_entity_segmentation,
    get_entity_suggestions,
    get_selection_rows,
    get_templates,
    property_labels_for,
    property_types_for,
    promote_validation_row,
    read_validation_label,
    rescore_run,
    resolve_entity_document,
    revert_entity_rows,
    revert_suboperation,
    select_value_catalog,
    set_selected_values,
    validation_summary,
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
    # The title alone: the sharedId is in the browser link and in the table, and
    # printing it beside the title only crowded the header.
    label = title + (f" · {language}" if language else "")
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
    """The entity's rows in one language group, narrowed by the table's filter.

    When the filter hides EVERY row of the group (a validation row under
    "regular rows only", a status the row no longer carries, a title search),
    the window still opens that entity: the operator clicked this row, so an
    empty panel with "no suggestions" is never the right answer. The unfiltered
    read is the fallback, and it is what makes the document/PDF reachable for a
    validation or not-processed row.
    """
    url = page_instance_url()
    rows = get_entity_suggestions(url, state["run_id"], shared_id, language, _current_filter(state))
    if rows:
        return rows
    return get_entity_suggestions(url, state["run_id"], shared_id, language)


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
        types = _property_types_for_suggestions(suggestions)
        _entity_suggestions(
            state,
            suggestions,
            _property_labels(suggestions),
            focus_property=focus_property,
            types=types,
            titles=_relationship_titles_for(suggestions, types),
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
    hide their undo. The same rule as the table applies — a deleted entity, a
    create-merge (its target is a dry-created placeholder) and a file copy
    revert at run level, not from this button. A CREATED entity is revertable:
    its undo deletes the entity the run minted. An ``update_and_delete`` merge
    is revertable too: its undo restores the target and re-creates the sources
    it deleted.
    """
    rows = get_entity_suggestions(page_instance_url(), state["run_id"], shared_id, language) or suggestions
    in_place = not any(s.op in NON_IN_PLACE_OPS for s in rows)
    # The table's own rule: an entity with at least one applied row that is not
    # run-level-only. That covers a whole-entity op (publish/unpublish) whose
    # undo the snapshot restore also performs, a created entity (deleted on
    # undo), not just property writes.
    return in_place and any(s.status is SuggestionStatus.APPLIED for s in rows)


def _entity_is_created(suggestions: list[Suggestion]) -> bool:
    """Whether this group's rows describe an entity the run CREATED (pure).

    A create row (``create``, or a ``create_and_delete`` merge) names an entity
    that did not exist before the Run, so the group has no pre-run state to
    restore and its undo is a deletion.
    """
    return any(s.op in CREATE_OPS for s in suggestions)


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
    if document is not None:
        pane.clear()
        with pane:
            ui.element("iframe").props(f"src={_viewer_src(state, shared_id, document.language)}").classes("w-full").style(
                "height: 100%; border: 0"
            )
        return
    # The entity's own rows carry no document (a `create` row's placeholder
    # never holds files): fall back to the SOURCE entity the rows name —
    # the entity whose PDF/fields the suggestion was derived from. Its
    # document IS the evidence the operator needs to review this row.
    rows = _entity_rows(state, shared_id, language)
    source_id = _source_entity_id(rows)
    if source_id:
        try:
            document = await resolve_entity_document(
                page_instance_url(), credentials.user, credentials.password, source_id, language
            )
        except Exception as exc:  # noqa: BLE001 — best-effort provenance, never an error state
            logger.info("no primary document on source {} for {}: {}", source_id, shared_id, exc)
            document = None
        if pane.is_deleted:
            return
        if document is not None:
            pane.clear()
            with pane:
                ui.label("Source document — the entity this suggestion was derived from").classes("text-caption text-grey-7")
                ui.element("iframe").props(f"src={_viewer_src(state, source_id, document.language)}").classes(
                    "w-full"
                ).style("height: 100%; border: 0")
            return
    await _render_segmentation_fallback(pane, credentials, shared_id, language)


def _source_entity_id(suggestions: list[Suggestion]) -> str | None:
    """The SOURCE entity the rows were derived from, or None (pure).

    A `create` row's ``shared_id`` is the ``dry-created-N`` placeholder — an
    entity Uwazi has yet to mint — so a document lookup on it can only fail.
    The script's optional ``source_shared_id`` names the entity whose PDF/fields
    the new one was derived from; its document IS the operator's evidence. The
    first named source wins (several sources are collapsed to the primary one).
    """
    for suggestion in suggestions:
        source = (suggestion.source_shared_id or "").strip()
        if source and source != suggestion.shared_id:
            return source
    return None


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
    created = _entity_is_created(rows)
    # The same open-in-Uwazi rule the review table uses: only a row that points
    # at a REAL entity page (not a dry-created placeholder, not a publish-only
    # change) gets the link, so the icon never opens a 404. A create row links
    # through the id a Run minted, exactly as its table row does.
    open_url = operation_open_url(
        shared_id,
        [s.op for s in rows],
        [language] if language else [],
        page_instance_url(),
        created_shared_id=next((s.created_shared_id for s in rows if s.created_shared_id), None),
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
                "Delete (undo create)" if created else "Revert this entity",
                icon="delete" if created else "undo",
                on_click=lambda: _revert_entity_clicked(state, shared_id),
            ).props(f"flat dense color={'negative' if created else 'indigo'}")
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


def _relationship_titles_for(
    suggestions: list[Suggestion],
    types: dict[str, str] | None = None,
) -> dict[str, str]:
    """``{sharedId: title}`` for the relationship targets these rows name (cached, best-effort).

    Only rows whose property is a ``relationship`` contribute ids, so a task
    with no relationship row costs no lookup at all.
    """
    from .value_pickers import relationship_titles

    resolved_types = types if types is not None else _property_types_for_suggestions(suggestions)
    relationship_rows = [s for s in suggestions if resolved_types.get(s.property_name or "") == RELATIONSHIP_TYPE]
    return relationship_titles(relationship_rows) if relationship_rows else {}


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


def _render_value(value: Any, property_type: str | None, titles: dict[str, str] | None = None) -> str:
    """Render one value for the window, using its TITLE map for relationships (pure)."""
    if property_type == RELATIONSHIP_TYPE:
        return render_relationship(value, titles)
    return full_value(value, property_type)


def _relationship_summary(suggestion: Suggestion, titles: dict[str, str] | None = None) -> None:
    """The relationship row's write summary: the accepted target titles (pure)."""
    selected = relationship_selection(suggestion.proposed_value, suggestion.rejected_values, suggestion.selected_values)
    unpredicted = [
        shared_id
        for shared_id in relationship_selection(suggestion.current_value, [], None)
        if shared_id not in set(selected)
    ]
    if unpredicted:
        _entity_value_label(
            "Not predicted, will be removed by this Run: "
            + ", ".join(render_relationship([shared_id], titles) for shared_id in unpredicted),
            "text-body2 text-orange-9",
        )
    recommended = relationship_selection(suggestion.proposed_value, [], None)
    if recommended:
        _entity_value_label(
            "Recommended: " + ", ".join(render_relationship([shared_id], titles) for shared_id in recommended),
            "text-caption text-grey-7",
        )
    _entity_value_label(
        "Will write: " + (", ".join(render_relationship([shared_id], titles) for shared_id in selected) or "— (nothing)"),
        "text-body2 text-teal",
    )


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
    titles: dict[str, str] | None = None,
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
        current_value=_render_value(suggestion.current_value, property_type, titles),
        proposed_value=_render_value(suggestion.proposed_value, property_type, titles),
        evidence=evidence,
    )


def _fix_prompt_clicked(state: dict[str, Any], correction: SuggestionCorrection) -> None:
    """Open the prompt chat attached to this wrong suggestion (correction mode).

    When the flagged row is HOLDOUT evidence, the fix is derived from the test
    set: the stored score stops being a fair unseen estimate and the card says
    so. The correction is still offered — an operator fixing a visible failure
    is the point of showing them — but the number is relabelled honestly.
    """
    try:
        from uwazi_admin_agent.drivers.web.run_service import validation_store

        store = validation_store(state["instance_url"], state["run_id"])
        validation = store.load_set()
        if validation is not None and correction.shared_id in validation.shared_ids:
            store.mark_contaminated(validation)
    except Exception as exc:  # noqa: BLE001 — the fix flow must not depend on the set
        logger.warning("could not mark the validation set contaminated: {}", exc)
    prompt_chat_dialog(
        state["run_id"],
        # The regeneration rewrites the whole review table; the operator must
        # not keep working on a page whose rows are being replaced underneath
        # them. Move to the tasks window, where the run's row shows the
        # `regenerating` state until the new script and table are committed —
        # the same behavior the page menu's prompt chat already has.
        on_run_started=lambda: ui.navigate.to(page_link("/app")),
        correction=correction,
    )


def _entity_suggestions(
    state: dict[str, Any],
    suggestions: list[Suggestion],
    labels: dict[str, str] | None = None,
    focus_property: str | None = None,
    types: dict[str, str] | None = None,
    titles: dict[str, str] | None = None,
) -> None:
    """The right-hand panel: the precomputed rows the Run heuristic will write.

    Each card is headed by the property's LABEL — what Uwazi calls it and what
    the operator knows it by (:func:`property_heading`). ``labels`` is the
    template's label map; without it the heading falls back to the raw name.
    The internal property name is NOT printed: the actions use it as an
    identifier, the operator never reads it.

    Each card carries the same per-property actions as the review table's
    suboperation row: Run (when the part can still be written on its own) and
    Revert (when that property has a completed in-place write to undo). The
    header's "Run this row"/"Revert this entity" remain the coarse actions.

    ``focus_property`` highlights the property the window was opened from (a
    suboperation's Edit icon) without hiding the entity's other rows.
    ``types`` maps property names to their template type so a date value reads
    as a calendar date rather than a raw epoch timestamp. ``titles`` maps
    related-entity sharedIds to their display titles, so a relationship value
    reads as the target entities instead of bare ids.
    """
    resolved = labels or {}
    property_types = types or {}
    titles = titles or {}
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
                        ui.label("Excluded").classes("text-subtitle2")
                        ui.label(skip_reason_label(suggestion.skip_reason)).classes("text-caption text-grey-7")
                    elif suggestion.op == MATCHED_OP:
                        ui.label(property_heading(suggestion.property_name, suggestion.op, resolved)).classes(
                            "text-subtitle2"
                        )
                    else:
                        ui.label(property_heading(suggestion.property_name, suggestion.op, resolved)).classes(
                            "text-subtitle2"
                        )
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
                    f"Current value in Uwazi: {_render_value(suggestion.current_value, property_type, titles)}",
                    "text-body2 text-grey-7",
                )
                _entity_value_label(
                    f"Suggested value: {_render_value(suggestion.proposed_value, property_type, titles)}",
                    "text-body2 text-teal-9",
                )
            if suggestion.op == SKIPPED_OP and suggestion.proposed_value:
                _entity_value_label(
                    f"Evidence for this exclusion: {suggestion.proposed_value}",
                    "text-body2 text-grey-7",
                )
            if suggestion.op in PROPERTY_OPS:
                property_type = property_types.get(suggestion.property_name or "")
                _entity_value_label(
                    f"Current value: {_render_value(suggestion.current_value, property_type, titles)}",
                    "text-body2 text-grey-7",
                )
                if property_type == RELATIONSHIP_TYPE:
                    from .value_pickers import relationship_picker

                    relationship_picker(state, suggestion, titles, _suggestion_is_runnable(suggestion))
                    _relationship_summary(suggestion, titles)
                elif property_type == SELECT_TYPE:
                    from .value_pickers import select_catalog, single_select_picker

                    selected = effective_selection(
                        suggestion.proposed_value, suggestion.rejected_values, suggestion.selected_values
                    )
                    single_select_picker(
                        state,
                        suggestion,
                        select_catalog(suggestion),
                        selected,
                        _suggestion_is_runnable(suggestion),
                    )
                    unpredicted = unpredicted_values(suggestion.current_value, selected)
                    if unpredicted:
                        _entity_value_label(
                            f"Not predicted, will be removed by this Run: {', '.join(unpredicted)}",
                            "text-body2 text-orange-9",
                        )
                    _entity_value_label(
                        f"Will write: {', '.join(selected) or '— (nothing)'}",
                        "text-body2 text-teal",
                    )
                elif property_type == MULTISELECT_TYPE:
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
                    # other non-runnable state is read-only.
                    editable = _suggestion_is_runnable(suggestion) or suggestion.discard_reason == ALL_VALUES_REJECTED_REASON
                    picker = ui.select(
                        {option["label"]: _option_text(option) for option in options},
                        multiple=True,
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
                        f"Suggested value: {_render_value(suggestion.proposed_value, property_type, titles)}",
                        "text-body2",
                    )
                if suggestion.context:
                    _entity_value_label(
                        f"Input text the suggestion was read from: {suggestion.context}",
                        "text-body2 text-grey-7",
                    )

    if merge_rows:
        status = operation_status([{"status": sub_status_label(row.status)} for row in merge_rows])
        status_color = RUN_STATUS_COLORS.get(status, "grey-7")
        for detail in merge_detail_rows(merge_rows, status=status, status_color=status_color):
            _entity_structural_card(detail)
    if move_rows:
        for detail in file_move_detail_rows(move_rows):
            _entity_structural_card(detail)
    corrections = [_suggestion_correction(s, resolved, property_types, titles) for s in suggestions if _can_fix_prompt(s)]
    if corrections or suggestions:
        _entity_fix_prompt_button(state, corrections, suggestions)


def _entity_fix_prompt_button(
    state: dict[str, Any],
    corrections: list[SuggestionCorrection],
    suggestions: list[Suggestion] | None = None,
) -> None:
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
        _entity_validation_button(state, suggestions or [])
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


def _entity_validation_button(state: dict[str, Any], suggestions: list[Suggestion]) -> None:
    """Add/remove THIS entity from the validation set (sits left of Fix the prompt).

    Removing is the promotion path: the entity leaves the holdout and its rows
    become regular, runnable work. Adding is offered only when the run HAS a set
    (the sample, its property and its labels were chosen at creation); adding an
    entity the generation turn already read contaminates the score and the card
    says so afterwards.
    """
    if not suggestions:
        return
    shared_id = suggestions[0].shared_id
    if not shared_id or shared_id.startswith("dry-created-"):
        return
    try:
        summary = validation_summary(state["instance_url"], state["run_id"])
    except Exception as exc:  # noqa: BLE001 — a missing set never blocks the window
        logger.warning("validation toggle unavailable: {}", exc)
        return
    if summary["state"] == "none":
        return
    try:
        in_set = entity_is_validation(state["instance_url"], state["run_id"], shared_id)
    except Exception:  # noqa: BLE001
        return
    if in_set:
        ui.button(
            "Remove from validation",
            icon="science",
            on_click=lambda: _remove_validation_clicked(state, shared_id),
        ).props("unelevated color=purple size=md").tooltip(
            "Take this entity out of the test set; its rows become regular, runnable work"
        )
    else:
        ui.button(
            "Add to validation",
            icon="science",
            on_click=lambda: _add_validation_clicked(state, shared_id),
        ).props("unelevated color=grey-8 size=md").tooltip(
            "Include this entity in the test set (marks the score as a regression score: the AI already read it)"
        )


def _remove_validation_clicked(state: dict[str, Any], shared_id: str) -> None:
    """Promote the entity out of the holdout, then repaint the table AND re-score.

    The promotion runs on the event loop (it is two local file/DB writes plus the
    score), and the UI work happens inside an explicit client so a closed window
    cannot break the refresh: the old handler called ``_refresh_all`` from the
    button's own slot, whose parent the entity-window re-render had just deleted
    ("The parent element this slot belongs to has been deleted"), so the table
    kept showing ``validate`` until a manual browser reload.
    """
    client = context.client
    try:
        promote_validation_row(state["instance_url"], state["run_id"], shared_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("remove from validation failed")
        ui.notify(f"Could not remove {shared_id} from validation: {exc}", type="negative", multi_line=True)
        return
    credentials = credentials_for_instance(state["instance_url"])
    background_tasks.create(
        _rescore_after_set_change(state, client, credentials, f"{shared_id} left the validation set"),
        name=f"rescore {state['run_id']}",
    )


async def _rescore_after_set_change(
    state: dict[str, Any],
    client: Any,
    credentials: SessionCredentials | None,
    message: str,
) -> None:
    """Recompute the score after the set changed, then repaint everything.

    The set's version moved, so the stored number was cleared: without this the
    card kept reading "not scored yet" and the operator had no way to know
    whether the promotion changed the measurement. The table must be reloaded
    regardless — the promoted rows' status/``is_validation`` changed, which is
    what makes the operation read ``update`` instead of ``validate``.
    """
    with client:
        _refresh_entity_window(state)
        _refresh_all(state)
        ui.notify(message, type="positive", multi_line=True)
    if credentials is None:
        with client:
            ui.notify(
                "The performance score was cleared with the set change — log in to Uwazi to recompute it.",
                type="warning",
                multi_line=True,
            )
        return
    try:
        # ``rescore_run`` is a coroutine (evaluate_run is async); it does its own
        # blocking work on a worker thread, so awaiting it here is enough.
        score = await rescore_run(
            state["instance_url"],
            state["run_id"],
            credentials.user,
            credentials.password,
        )
    except Exception as exc:  # noqa: BLE001 — the set change landed; the score is best-effort
        logger.warning("re-scoring after a set change failed for {}: {}", state["run_id"], exc)
        with client:
            ui.notify(
                f"The set changed, but the score could not be recomputed: {exc}",
                type="warning",
                multi_line=True,
            )
        return
    with client:
        _refresh_all(state)
        if score is not None:
            ui.notify(f"Performance re-scored: {score.value_text()}", type="positive")


def _add_validation_clicked(state: dict[str, Any], shared_id: str) -> None:
    credentials = credentials_for_instance(state["instance_url"])
    if credentials is None:
        ui.notify("Not logged in to Uwazi", type="negative")
        return

    def _confirm() -> None:
        client = context.client
        background_tasks.create(
            _add_validation(state, client, credentials, shared_id),
            name=f"add validation {shared_id}",
        )

    _simple_confirm(
        "Add this entity to the validation set?",
        f"{shared_id} was read by the AI while it authored this script. Adding it now turns the score "
        "into a regression score, not a fair estimate on unseen data.",
        _confirm,
    )


async def _add_validation(state: dict[str, Any], client: Any, credentials: SessionCredentials, shared_id: str) -> None:
    """Freeze the entity's label, mark its rows as evidence, then re-score.

    The label is read BEFORE the set changes (the row values are overwritten by
    the marking), and the rows are marked so the table shows ``validate`` — the
    half that was missing, which made the button look like it did nothing.
    """
    try:
        label = await read_validation_label(
            state["instance_url"], state["run_id"], shared_id, credentials.user, credentials.password
        )
        updated = await asyncio.to_thread(add_validation_sample, state["instance_url"], state["run_id"], shared_id, label)
    except Exception as exc:  # noqa: BLE001
        logger.exception("add to validation failed")
        with client:
            ui.notify(f"Could not add {shared_id} to validation: {exc}", type="negative", multi_line=True)
        return
    if updated is None:
        with client:
            ui.notify("This run has no validation set to add the entity to.", type="warning")
        return
    with client:
        _refresh_entity_window(state)
        _refresh_all(state)
        ui.notify(
            f"{shared_id} added — its rows now read 'validate'. Recomputing the performance score.",
            type="positive",
            multi_line=True,
        )
    await _rescore_after_set_change(state, client, credentials, f"{shared_id} is now part of the test set")


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
        _entity_value_label(f"Current value: {detail['current']}", "text-body2 text-grey-7")
        _entity_value_label(f"Suggested value: {detail['proposed']}", "text-body2")


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

    # A create task's rows live in the language-independent group; the window is
    # usually open on it, but a create row can also be opened via its language.
    rows = _entity_rows(state, shared_id, None) or []
    title, message = revert_entity_confirmation(
        shared_id,
        tooltip_text,
        merge=entity_is_merge(page_instance_url(), state["run_id"], shared_id),
        created=_entity_is_created(rows),
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
                if _entity_is_created(get_entity_suggestions(page_instance_url(), state["run_id"], shared_id)):
                    ui.notify(f"Deleted {shared_id} — the run created it, so the creation is undone", type="positive")
                else:
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
