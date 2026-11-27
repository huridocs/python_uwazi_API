"""Review-page module split out of the original ``review_page`` (behavior preserved)."""

from __future__ import annotations

from functools import partial
from typing import Any

from nicegui import background_tasks, context, ui

from uwazi_admin_agent.drivers.web.header import instance_badge
from uwazi_admin_agent.drivers.web.instances import page_link
from uwazi_admin_agent.drivers.web.operation_gate import (
    set_progress,
)
from uwazi_admin_agent.drivers.web.prompt_chat_dialog import prompt_chat_dialog
from uwazi_admin_agent.drivers.web.review_view import (
    OP_LABELS,
    filter_from_controls,
    filter_options,
    language_filter_options,
    property_filter_options,
    run_button_label,
    section_toggle_label,
    select_all_label,
)
from uwazi_admin_agent.drivers.web.run_service import (
    current_pass,
    get_filter_counts,
    get_run,
)
from uwazi_admin_agent.drivers.web.session import (
    bind_client_instance,
    page_instance_url,
    session_is_active,
)
from uwazi_admin_agent.drivers.web.task_dialogs import script_dialog
from uwazi_admin_agent.drivers.web.theme import install_theme

from .core import (
    _STATUS_LABELS,
    _change_table,
    _check_drift_now,
    _discard_from_state,
    _drift_banner,
    _performance_row,
    _poll_gate_toast,
    _poll_progress,
    _poll_run_claim,
    _refresh_all,
    _refresh_expand_button,
    _refresh_selection_counts,
    _reset_global_selection,
    _revert_from_state,
    _run_from_state,
    _summary_row,
    _toggle_expand_all,
    _toggle_performance,
    _toggle_select_all,
    _update_table_now,
)
from .entity_window import _merged_labels, _merged_types, _template_labels, _template_types

_REVIEW_CSS = """
body { background:var(--uw-parchment); font-family:var(--uw-font-sans); color:var(--uw-ink); overflow-x:hidden; }
.nicegui-content { padding: 0; }
.uw-header { background:var(--uw-paper) !important; color:var(--uw-ink) !important; box-shadow:none !important;
             border-bottom:1px solid var(--uw-border); }
.uw-header .q-link { color:var(--uw-carbon); }
.uw-page { max-width:1360px; margin:0 auto; padding:24px 24px 96px; display:flex;
           flex-direction:column; gap:16px; }
.uw-card {
  background:var(--uw-paper); border:1px solid var(--uw-border);
  border-radius:var(--uw-radius); box-shadow:var(--uw-shadow-md); overflow:hidden;
}
.uw-card-header { padding:16px 20px; border-bottom:1px solid var(--uw-border-soft); }
.uw-card-title { font-size:22px; line-height:1.2; font-weight:700; color:var(--uw-ink); }
.uw-summary { font-size:13px; color:var(--uw-ink-tertiary); }
.uw-info-banner {
  background:var(--uw-warning-light);
  border:1px solid color-mix(in srgb, var(--uw-warning) 35%, var(--uw-border));
  border-radius:8px; padding:12px 16px; display:flex; align-items:center;
}
.uw-banner-title { font-weight:700; font-size:13px; color:var(--uw-ink); }
.uw-banner-text { font-size:13px; color:var(--uw-ink-secondary); }
.uw-banner-value { color:var(--uw-ink); }
.uw-search-row { padding:12px 20px 0; }
.uw-filter-row { padding:14px 20px; border-bottom:1px solid var(--uw-border-soft); }
.filter-box {
  border:1px solid var(--uw-border-soft); border-radius:8px; background:var(--uw-paper);
  padding:6px 10px 5px 10px; min-height:52px; display:flex; flex-direction:column;
  justify-content:center; position:relative;
}
.filter-placeholder { position:absolute; left:10px; top:29px; font-size:13px; color:var(--uw-ink);
                      pointer-events:none; }
.filter-caption { font-size:10px; line-height:1.2; font-weight:700; letter-spacing:.06em;
                  text-transform:uppercase; color:var(--uw-ink-tertiary); }
.filter-select .q-field__control { min-height:26px; height:auto; padding:0; }
.filter-select .q-field__native, .filter-select .q-field__input { padding:0; min-height:24px;
                                                                  color:var(--uw-ink); font-size:13px;
                                                                  flex-wrap:wrap; align-content:center;
                                                                  row-gap:2px; max-height:72px;
                                                                  overflow-y:auto; }
.filter-select .q-field__append { height:auto; }
.filter-select .q-field__bottom { display:none; }
.filter-select .q-chip { font-size:11px; max-width:100%; }
.filter-select .q-chip .q-chip__content { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.filter-select .q-field__control:before, .filter-select .q-field__control:after { display:none; }
.reset-link { color:var(--uw-ink-tertiary) !important; font-size:13px; }
.uw-table .q-table__middle { max-height:70vh; overflow-y:auto; overflow-x:hidden; }
.uw-table .q-table { table-layout:fixed; width:100%; }
.uw-table thead tr th { position:sticky; top:0; z-index:3; background:var(--uw-warm); text-transform:uppercase;
                        font-size:11px; font-weight:700; color:var(--uw-ink-tertiary); padding:10px 12px;
                        border-bottom:1px solid var(--uw-border); }
.uw-table tbody td { padding:10px 12px; border-bottom:1px solid var(--uw-border-soft); vertical-align:middle;
                     overflow:hidden; }
.uw-table thead tr th:first-child, .uw-table tbody td:first-child {
  width:52px; min-width:52px; max-width:52px; padding-left:12px; padding-right:4px; }
.uw-table thead tr th:first-child .q-checkbox, .uw-table tbody td:first-child .q-checkbox { margin:0; }
.uw-table.review-no-selectable thead th:first-child { visibility:hidden; }
/* Fixed layout reads the column widths from the HEADER row: Quasar puts the
   per-column ``style`` only on the body cells, so the widths are declared here
   (one rule per column, in the same order as the columns list). */
.uw-table thead tr th:nth-child(2) { width:36px; }
.uw-table thead tr th:nth-child(3) { width:190px; }
.uw-table thead tr th:nth-child(4) { width:120px; }
.uw-table thead tr th:nth-child(5) { width:auto; }
.uw-table thead tr th:nth-child(6) { width:132px; }
.uw-table thead tr th:nth-child(7) { width:176px; }
.uw-table thead tr th:nth-child(8) { width:226px; }
.uw-table tbody tr:hover { background:var(--uw-warm); }
.uw-table tbody tr.child-row:hover { background:var(--uw-selected); }
.uw-table .q-table__sort-icon { font-size:14px; opacity:.7; }
.uw-table .q-checkbox { margin:0; }
.entity-link { color:var(--uw-carbon); font-weight:500; text-decoration:none; cursor:pointer; }
.entity-link:hover { text-decoration:underline; }
.truncate { max-width:100%; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.rec-cell { color:var(--uw-ink-secondary); font-size:13px; }
.rec-pair { display:flex; flex-direction:column; gap:2px; min-width:0; }
.rec-line { display:flex; align-items:baseline; gap:6px; min-width:0; }
.rec-tag { flex:0 0 auto; width:74px; font-size:10px; font-weight:700; letter-spacing:.04em;
           text-transform:uppercase; color:var(--uw-ink-tertiary); }
.rec-value { font-size:13px; color:var(--uw-ink); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.rec-current .rec-line { align-items:flex-start; }
.rec-current .rec-value {
  color:var(--uw-ink-tertiary);
  white-space:pre-wrap; overflow-wrap:anywhere; text-overflow:clip;
  max-height:12em; overflow-y:auto; overflow-x:hidden;
}
.rec-values { display:flex; flex-wrap:wrap; gap:4px; min-width:0; }
.rec-values .q-chip { height:auto; min-height:20px; padding:1px 8px; font-size:11px; }
.rec-extra { font-size:12px; color:var(--uw-ink-secondary); }
.subs-context { font-size:12px; line-height:1.45; color:var(--uw-ink-secondary); white-space:pre-wrap;
                overflow-wrap:anywhere; border-left:3px solid var(--uw-border-soft); padding-left:8px;
                user-select:text; }
.context-row td { background:var(--uw-warm); padding-top:4px; padding-bottom:8px; }
.op-pill { border-radius:6px; font-size:11px; font-weight:600; padding:2px 8px; }
.status-pill { border-radius:999px; font-size:11px; font-weight:600; padding:3px 9px; line-height:1.3; }
.status-pending { background:var(--uw-warning-light); color:#92400E; }
.status-applied { background:var(--uw-success-light); color:#166534; }
.status-discarded { background:var(--uw-vellum); color:var(--uw-ink-tertiary); }
.status-negative { background:var(--uw-emphasis-tint); color:var(--uw-emphasis); }
.status-neutral { background:var(--uw-vellum); color:var(--uw-ink-tertiary); }
.status-progress { width:120px; }
.actions-cell { gap:6px; }
.apply-btn { border-radius:6px; font-weight:600; font-size:12px; padding:3px 12px; }
.row-actions { visibility:hidden; }
tr:hover .row-actions { visibility:visible; }
.child-row { background:var(--uw-warm); }
.child-row td { background:var(--uw-warm); border-bottom:1px solid var(--uw-border); padding-top:8px; padding-bottom:8px; }
.child-row td:first-child { border-left:2px solid var(--uw-ink); }
.child-target { padding-left:32px; font-size:13px; color:var(--uw-ink-secondary); }
.child-row > td.expanded-cell { padding:0 !important; background:var(--uw-warm); }
.sub-list { display:flex; flex-direction:column; gap:8px; padding:10px 16px 14px 112px; }
.sub-item {
  background:var(--uw-paper); border:1px solid var(--uw-border);
  border-radius:var(--uw-radius); padding:12px 14px;
}
.sub-item-structural { border-style:dashed; }
.sub-item-head { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
.sub-item-title { font-size:13px; font-weight:600; color:var(--uw-ink); }
.sub-item-caption { margin-top:2px; font-size:11px; color:var(--uw-ink-tertiary); }
.sub-item-actions { margin-left:auto; }
.sub-item-values { display:flex; flex-direction:column; gap:8px; margin-top:8px; }
.sub-value {
  display:grid; grid-template-columns:96px minmax(0,1fr);
  gap:12px; align-items:start;
}
.sub-value-label {
  display:block; margin:0; padding-top:1px; font-size:10px; font-weight:700;
  letter-spacing:.06em; text-transform:uppercase; color:var(--uw-ink-tertiary);
}
.sub-value-text {
  min-width:0; font-size:13px; line-height:1.45; color:var(--uw-ink-secondary);
  white-space:pre-wrap; overflow-wrap:anywhere;
}
.sub-current { max-height:12em; overflow-y:auto; color:var(--uw-ink); }
.sub-proposed { max-height:8em; overflow-y:auto; color:var(--uw-ink); }
.sub-values { display:flex; flex-wrap:wrap; gap:4px; }
.sub-item-context { margin-top:8px; }
.sub-item-context .sub-value-label { display:block; margin-bottom:2px; }
.sub-item-note { margin-top:6px; font-size:11px; color:var(--uw-ink-muted); }
@media (max-width: 900px) {
  .sub-list { padding-left:24px; }
}
.lang-pill { border-radius:4px; font-size:10px; padding:1px 6px; }
.wide-tooltip { max-width:480px; white-space:pre-wrap; }
.uw-card-footer { padding:10px 20px; border-top:1px solid var(--uw-border-soft); }
.uw-page-label { font-size:12px; color:var(--uw-ink-tertiary); }
.uw-card-footer .page-btn { min-width:30px; border-radius:6px; color:var(--uw-ink-tertiary) !important; font-size:13px; }
.uw-card-footer .page-btn-active, .uw-card-footer .page-btn-active * {
  background:var(--uw-ink) !important; color:#fff !important; }
.page-input { width:64px; }
.page-input .q-field__control { height:30px; min-height:30px; }
.sync-button { font-size:12px; font-weight:600; padding:4px 14px; }
.sync-button .q-icon { font-size:18px; }
.sync-dot { width:8px; height:8px; border-radius:999px; display:inline-block;
            background:var(--uw-ink-muted); flex:0 0 auto; }
.sync-dot-pulse { animation: syncpulse 1.1s ease-in-out infinite; }
@keyframes syncpulse { 0%,100%{opacity:.35} 50%{opacity:1} }
.sync-label { font-size:12px; color:var(--uw-ink-tertiary); }
.uw-run-banner {
  display:flex; align-items:flex-start; gap:12px; width:100%;
  padding:12px 16px; border-radius:var(--uw-radius);
  border:1px solid color-mix(in srgb, var(--uw-warning) 35%, var(--uw-border));
  background:var(--uw-warning-light);
}
.uw-run-banner .q-spinner { margin-top:2px; }
.uw-run-banner-title { font-size:13px; font-weight:700; color:var(--uw-ink); }
.uw-run-banner-note { font-size:12px; color:var(--uw-ink-secondary); white-space:pre-wrap; overflow-wrap:anywhere; }
/* The claim makes the table read-only: QTable's header select-all checkbox is
   internal, so the class disables it here; row/child controls carry :disable. */
.uw-table.review-readonly thead th:first-child .q-checkbox { pointer-events:none; opacity:.4; }
.uw-table.review-readonly tbody .q-checkbox { pointer-events:none; opacity:.55; }
.uw-bulk-bar { background:var(--uw-paper); border-top:1px solid var(--uw-border); padding:10px 24px;
               box-shadow:0 -2px 8px rgba(0,0,0,0.04); }
.uw-selection-label { font-size:13px; font-weight:700; color:var(--uw-ink); }
.uw-run-button { background:var(--uw-ink) !important; color:#fff !important; }
.uw-select-all { color:var(--uw-carbon) !important; font-size:13px; }
.uw-discard-button {
  color:var(--uw-emphasis) !important;
  border-color:color-mix(in srgb, var(--uw-emphasis) 35%, transparent) !important;
}
.uw-bulk-sticky { z-index:1000; }
.brand-mark { width:28px; height:28px; border-radius:6px; background:var(--uw-emphasis); color:#fff;
              display:flex; align-items:center; justify-content:center; font-weight:800; font-size:16px; }
.brand-name { font-size:16px; font-weight:700; color:var(--uw-ink); }
.breadcrumb-link { color:var(--uw-carbon); font-size:13px; }
.breadcrumb-chevron { color:var(--uw-ink-muted); font-size:16px; }
.breadcrumb-current { color:var(--uw-ink); font-size:13px; font-weight:500; }
.settings-btn { color:var(--uw-ink) !important; }
"""


def review_page(run_id: str, instance_url: str) -> None:
    """Render the review page for one run (login required).

    ``instance_url`` is the configured instance the path prefix named; a
    session logged in to another instance (or none) is sent to that instance's
    login page.
    """
    if not session_is_active(instance_url):
        ui.navigate.to(page_link("/", instance_url))
        return
    install_theme()
    ui.add_css(_REVIEW_CSS)
    _build_page(run_id)


def _build_page(run_id: str) -> None:
    bind_client_instance()
    instance = page_instance_url()
    if not instance:
        ui.navigate.to(page_link("/"))
        return
    try:
        detail = get_run(instance, run_id)
    except Exception as exc:  # noqa: BLE001 — a missing run shows an error, never a crash
        with ui.column().classes("w-full items-center justify-center min-h-screen"):
            ui.label(f"Cannot open review for {run_id!r}: {exc}").classes("text-body1 text-negative")
            ui.button("Back to tasks", on_click=lambda: ui.navigate.to(page_link("/app"))).props("flat")
        return

    state: dict[str, Any] = {
        "run_id": run_id,
        # The instance this page is bound to, captured once: background tasks
        # and the gate/toast pollers must keep using it even if the session
        # later changes, and another instance's work must never show here.
        "instance_url": instance,
        # Instance-bound progress reporter for the gate's live toast.
        "_progress": partial(set_progress, instance),
        "offset": 0,
        "total": 0,
        "status": [],
        "op": [],
        "property": "",
        "language": "",
        "text": "",
        # The filter row is visible when the page opens; the free-text search
        # stays behind the settings menu (the dropdowns are the normal way to
        # narrow a run, a text scan is an occasional tool).
        "_filters_visible": True,
        "_search_visible": False,
        # The table's sort lives in the COLUMN HEADERS. 'recorded' keeps the
        # script's order until a header is clicked; a new column starts A->Z
        # (Target/Template/Operation) or newest-first for a date, and clicking
        # it again reverses. The store orders the WHOLE filtered set, not just
        # this page.
        "sort_by": "recorded",
        "sort_desc": False,
        "loading": False,
        # The SUBOPERATION selection: the suggestion rows the operator ticked,
        # held as row ids because that is the only identity finer than an
        # (entity, language) pair — "run this one property of this one
        # language". The parent operation's tick is DERIVED from its parts, so
        # the two levels cannot disagree.
        "_checked": set(),
        # Whole-result selection (the footer link): every suboperation matching
        # the current filter, across every page.
        "_select_all": False,
        "_global_refs": {},
        "_global_applied_ids": set(),
        # Which operations are expanded (the table's ``expanded`` prop is the
        # source of truth).
        "_expanded": set(),
        # Set while a filter control is reset programmatically.
        "_suppress_filter_events": False,
    }

    with ui.header().classes("uw-header items-center justify-between"):
        _top_bar(state, run_id, detail, instance)

    with ui.column().classes("uw-review-shell w-full items-center"):
        with ui.element("div").classes("uw-page uw-review-page w-full"):
            _performance_row(state)
            _run_state_banner(state)
            with ui.element("div").classes("uw-card uw-review-card w-full"):
                with ui.row().classes("uw-card-header w-full items-center justify-between no-wrap"):
                    _summary_row(state)
                    with ui.row().classes("items-center gap-4 no-wrap"):
                        _drift_banner(state)
                with ui.column().classes("uw-search-row w-full") as search_section:
                    state["_search_section"] = search_section
                    _search_bar(state)
                with ui.column().classes("w-full") as filters_section:
                    state["_filters_section"] = filters_section
                    _filter_bar(state)
                # The table and its in-card footer.
                _change_table(state)
            # The selection-driven bulk bar: hidden until something is checked.
            _action_bar(state)
            # Apply the initial visibility the controls advertise: filters ON,
            # search off. Entering the page ALWAYS resets to this.
            state["_filters_section"].set_visibility(state["_filters_visible"])
            state["_search_section"].set_visibility(state["_search_visible"])
            _refresh_selection_counts(state)

    if current_pass(page_instance_url(), run_id) is None:
        ui.notify(
            "No scan recorded yet — the automatic scan right after generation may still be running",
            type="info",
        )

    # Paint the claim that was already live when the page opened (a
    # regeneration started in another window, or a retry from the tasks page),
    # then keep the page in step while it runs.
    _poll_run_claim(state)
    ui.timer(3.0, lambda: _poll_progress(state))
    # The operation gate's fast poller: while a Run/Revert/refresh is held, this
    # keeps the live progress toast alive with the live fraction ("Running —
    # 13/1230 suboperations…"); idle, it does nothing.
    ui.timer(0.8, lambda: _poll_gate_toast(state))
    # The per-run claim poller: generation/regeneration happen outside the write
    # gate, so without this the review page kept showing a stale, actionable
    # table while the planner was rewriting it.
    ui.timer(1.5, lambda: _poll_run_claim(state))


def _top_bar(state: dict[str, Any], run_id: str, detail: Any, instance: str) -> None:
    """The white top bar: service wordmark and breadcrumb, then settings."""
    with ui.row().classes("items-center gap-6 no-wrap"):
        with ui.row().classes("items-center gap-2 no-wrap"):
            with ui.element("div").classes("brand-mark"):
                ui.label("u")
            ui.label("Uwazi Admin Agent").classes("brand-name")
        with ui.row().classes("items-center gap-1 no-wrap"):
            ui.link("Tasks", page_link("/app")).classes("breadcrumb-link")
            ui.icon("chevron_right").classes("breadcrumb-chevron")
            ui.label(run_id).classes("breadcrumb-current")
            state["_status_badge"] = ui.badge(detail.status.value).props("color=grey-7").classes("q-ml-sm status-pill")
    with ui.row().classes("items-center gap-2 no-wrap"):
        instance_badge(instance)
        _header_menu(state, run_id, has_script=bool(detail.script))


def _header_menu(state: dict[str, Any], run_id: str, *, has_script: bool = False) -> None:
    """The Settings menu: every occasional control, one click away.

    It holds the search toggle, the filters toggle, the run's prompt and
    script, the performance banner, and the way back to the tasks list. The
    controls that act on the table (expand/collapse, pagination) live in the
    card where the table is. The targeted instance is NOT here: it sits in the
    top bar (:func:`instance_badge`), where two open windows can be told apart
    without opening a menu.
    """
    with (
        ui.button(text="Settings", icon="settings")
        .props("outline no-caps color=grey-9")
        .classes("settings-btn uw-btn-compact") as button
    ):
        with ui.menu() as menu:
            state["_filters_item"], state["_filters_label"] = _menu_item(
                "filter_alt",
                section_toggle_label("filters", bool(state.get("_filters_visible", False))),
                lambda: _toggle_section(state, "filters"),
            )
            state["_search_item"], state["_search_label"] = _menu_item(
                "search",
                section_toggle_label("search", bool(state.get("_search_visible", False))),
                lambda: _toggle_section(state, "search"),
            )
            state["_performance_item"], state["_performance_menu_label"] = _menu_item(
                "speed",
                "Show performance",
                lambda: _toggle_performance(state),
            )
            _menu_item(
                "sync",
                "Check drift",
                lambda: background_tasks.create(_check_drift_now(state, client=context.client)),
            )
            state["_update_table_item"], state["_update_table_menu_label"] = _menu_item(
                "cloud_sync", "Sync with Uwazi", lambda: _update_table_clicked(state)
            )
            ui.separator()
            _menu_item(
                "description",
                "Show prompt",
                lambda: prompt_chat_dialog(
                    run_id,
                    # Stay on the review page: the run claim paints the
                    # read-only banner (and disables every mutating control),
                    # and this callback repaints the committed table when the
                    # regeneration finishes. Navigating away hid that state
                    # from the one page whose rows were being rewritten.
                    on_table_changed=lambda: _refresh_all(state),
                ),
            )
            if has_script:
                _menu_item("code", "Show script", lambda: script_dialog(run_id))
            ui.separator()
            _menu_item("arrow_back", "Back to tasks", lambda: ui.navigate.to(page_link("/app")))
    button.on("click", menu.open)


def _menu_item(icon: str, text: str, on_click: Any) -> tuple[Any, Any]:
    """One menu item: an icon and a label.

    Returns the item and its label: the item can be disabled while its action
    runs, and the label's text is what the section items rewrite when the
    section they toggle changes state.
    """
    with ui.menu_item(on_click=on_click) as item:
        ui.icon(icon).classes("q-mr-sm")
        label = ui.label(text)
    return item, label


def _toggle_section(state: dict[str, Any], section: str) -> None:
    """Show or hide one collapsible section, and relabel its menu item."""
    visible = not bool(state.get(f"_{section}_visible"))
    state[f"_{section}_visible"] = visible
    container = state.get(f"_{section}_section")
    if container is not None and not container.is_deleted:
        container.set_visibility(visible)
    label = state.get(f"_{section}_label")
    if label is not None and not label.is_deleted:
        label.text = section_toggle_label(section, visible)
        label.update()
    if section == "search" and not visible:
        # Hiding the search means the text narrowing is over: leaving it active
        # would keep the table filtered by a control the operator can no longer
        # see (and cannot clear without reopening the section).
        _clear_search(state)


def _clear_search(state: dict[str, Any]) -> None:
    """Clear the free-text filter and its input, then refresh the table."""
    if not (state.get("text") or ""):
        return
    state["text"] = ""
    widget = state.get("_text_input")
    if widget is not None and not widget.is_deleted:
        state["_suppress_filter_events"] = True
        try:
            widget.value = ""
        finally:
            state["_suppress_filter_events"] = False
    state["offset"] = 0
    _reset_global_selection(state)
    _refresh_all(state)


def _update_table_clicked(state: dict[str, Any]) -> None:
    """Start the Uwazi sync from the menu, with the menu item disabled while it runs."""
    item = state.get("_update_table_item")
    if item is not None and not item.is_deleted:
        item.disable()
    background_tasks.create(_update_table_now(state, client=context.client))


def _search_bar(state: dict[str, Any]) -> None:
    """The free-text search, hidden until the settings menu reveals it."""
    state["_text_input"] = (
        ui.input(
            "Search title / sharedId",
            value=state["text"],
            on_change=lambda e: _set_filter(state, "text", e.value or ""),
        )
        .props("dense outlined clearable")
        .classes("w-full")
    )


def _filter_bar(state: dict[str, Any]) -> None:
    """The filter row: four labelled selects, Reset and Expand/Collapse all."""
    labels_by_template = _template_labels(state["run_id"])
    state["_template_labels"] = labels_by_template
    state["_labels"] = _merged_labels(labels_by_template)
    types_by_template = _template_types(state["run_id"])
    state["_template_types"] = types_by_template
    state["_types"] = _merged_types(types_by_template)
    counts = get_filter_counts(
        page_instance_url(),
        state["run_id"],
        filter_from_controls(
            property_name=state["property"],
            language=state["language"],
            limit=100000,
        ),
    )
    property_options = property_filter_options(state["_labels"], counts.get("property", {}), state["property"])
    property_options[""] = "Any property"
    language_options = language_filter_options(counts.get("language", {}), state["language"])
    language_options[""] = "Any language"

    with ui.row().classes("uw-filter-row w-full items-stretch gap-3 wrap"):
        with ui.element("div").classes("filter-box").style("width:248px"):
            ui.label("Property").classes("filter-caption")
            state["_property_select"] = (
                ui.select(
                    property_options,
                    value=state["property"],
                    on_change=lambda e: _set_filter(state, "property", e.value or ""),
                )
                .props("borderless dense hide-bottom-space options-dense")
                .classes("filter-select w-full")
            )
        with ui.element("div").classes("filter-box").style("width:180px"):
            ui.label("Language").classes("filter-caption")
            state["_language_select"] = (
                ui.select(
                    language_options,
                    value=state["language"],
                    on_change=lambda e: _set_filter(state, "language", e.value or ""),
                )
                .props("borderless dense hide-bottom-space options-dense")
                .classes("filter-select w-full")
            )
        # Operation and Status are MULTI-selects: an operator often wants to
        # see two statuses (or two operations) at once. An empty selection is
        # the unfiltered state; the placeholder says so.
        with ui.element("div").classes("filter-box").style("width:300px"):
            ui.label("Operation").classes("filter-caption")
            state["_op_select"] = (
                ui.select(
                    filter_options(OP_LABELS, counts.get("op", {}), state["op"]),
                    multiple=True,
                    value=state["op"],
                    on_change=lambda e: _set_filter(state, "op", e.value or []),
                )
                .props("borderless dense hide-bottom-space options-dense use-chips clearable")
                .classes("filter-select w-full")
            )
            op_placeholder = ui.label("Any operation").classes("filter-placeholder")
            op_placeholder.bind_visibility_from(state["_op_select"], "value", backward=lambda v: not v)
        with ui.element("div").classes("filter-box").style("width:220px"):
            ui.label("Status").classes("filter-caption")
            state["_status_select"] = (
                ui.select(
                    filter_options(_STATUS_LABELS, counts.get("status", {}), state["status"]),
                    multiple=True,
                    value=state["status"],
                    on_change=lambda e: _set_filter(state, "status", e.value or []),
                )
                .props("borderless dense hide-bottom-space options-dense use-chips clearable")
                .classes("filter-select w-full")
            )
            status_placeholder = ui.label("Any status").classes("filter-placeholder")
            status_placeholder.bind_visibility_from(state["_status_select"], "value", backward=lambda v: not v)
        with ui.row().classes("items-center gap-3 no-wrap ml-auto"):
            ui.button("Reset", on_click=lambda: _reset_filters(state)).props("flat dense no-caps").classes("reset-link")
            _expand_all_button(state)


def _expand_all_button(state: dict[str, Any]) -> None:
    """Expand or collapse every row, in the exact spot the density toggle held."""
    state["_expand_button"] = (
        ui.button("Expand all", icon="unfold_more", on_click=lambda: _toggle_expand_all(state))
        .props("outline dense no-caps color=grey-9")
        .classes("expand-all-btn")
    )
    _refresh_expand_button(state)


def _set_filter(state: dict[str, Any], key: str, value: Any) -> None:
    if state.get("_suppress_filter_events"):
        # A programmatic reset of a control (see _reset_filters): the handler
        # must not re-enter the filter logic.
        return
    state[key] = value
    state["offset"] = 0
    _reset_global_selection(state)
    _refresh_all(state)


def _reset_filters(state: dict[str, Any]) -> None:
    """Clear EVERY filter: the state, and the control that shows it."""
    state.update(
        {
            "status": [],
            "op": [],
            "property": "",
            "language": "",
            "text": "",
            "offset": 0,
        }
    )
    widgets = {
        "property": "",
        "language": "",
        "op": [],
        "status": [],
    }
    state["_suppress_filter_events"] = True
    try:
        for key, empty in widgets.items():
            widget = state.get(f"_{key}_select")
            if widget is not None and not widget.is_deleted:
                widget.value = empty
        text_input = state.get("_text_input")
        if text_input is not None and not text_input.is_deleted:
            text_input.value = ""
    finally:
        state["_suppress_filter_events"] = False
    _reset_global_selection(state)
    _refresh_all(state)


def _run_state_banner(state: dict[str, Any]) -> None:
    """The read-only banner shown while this run has a live claim.

    Hidden by default so the page keeps its calm layout; the claim poller
    paints the title and the live stage (generation narrates what the LLM is
    doing) and makes it visible. It sits above the card, where it cannot be
    confused with the table's own filtering chrome.
    """
    banner = ui.element("div").classes("uw-run-banner w-full")
    state["_run_banner"] = banner
    with banner:
        state["_run_banner_spinner"] = ui.spinner(size="sm", color="primary")
        with ui.column().classes("gap-0 grow"):
            state["_run_banner_title"] = ui.label("").classes("uw-run-banner-title")
            state["_run_banner_note"] = ui.label("").classes("uw-run-banner-note")
    banner.set_visibility(False)


def _action_bar(state: dict[str, Any]) -> None:
    """The always-visible sticky bulk-action bar.

    The buttons act on the checked rows; with nothing checked they stay
    disabled so the bar never changes the table's height while filtering or
    paging. The counts describe the selection the operator just made.
    """
    sticky = ui.page_sticky(position="bottom", y_offset=0, expand=True).classes("uw-bulk-sticky")
    state["_bulk_bar"] = sticky
    with sticky:
        with ui.row().classes("uw-bulk-bar w-full items-center gap-3 no-wrap"):
            state["_selection_label"] = ui.label("0 selected").classes("uw-selection-label")
            state["_run_button"] = (
                ui.button(run_button_label(0), icon="play_arrow", on_click=lambda: _run_from_state(state))
                .props("unelevated no-caps color=primary")
                .classes("uw-run-button")
            )
            state["_bulk_revert_button"] = ui.button("Revert", on_click=lambda: _revert_from_state(state)).props(
                "outline no-caps color=grey-9"
            )
            # The bulk Discard: marking the checked operations as "will not be
            # executed" one by one is not workable on a table of thousands, so
            # the selection carries it too.
            state["_bulk_discard_button"] = (
                ui.button("Discard", on_click=lambda: _discard_from_state(state))
                .props("outline no-caps color=negative")
                .classes("uw-discard-button")
            )
            state["_select_all_button"] = (
                ui.button(select_all_label(0, False), on_click=lambda: _toggle_select_all(state))
                .props("flat no-caps")
                .classes("uw-select-all")
            )
    _refresh_selection_counts(state)


__all__ = ["review_page"]
