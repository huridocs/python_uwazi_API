"""Review-page module split out of the original ``review_page`` (behavior preserved)."""

from __future__ import annotations

from functools import partial
from typing import Any

from nicegui import background_tasks, context, ui
from starlette.requests import Request

from uwazi_admin_agent.drivers.web.instances import page_link, page_target
from uwazi_admin_agent.drivers.web.operation_gate import (
    set_progress,
)
from uwazi_admin_agent.drivers.web.operations_view import (
    any_property_option,
    property_options,
)
from uwazi_admin_agent.drivers.web.prompt_chat_dialog import prompt_chat_dialog
from uwazi_admin_agent.drivers.web.review_view import (
    OP_LABELS,
    OPERATION_SORT_OPTIONS,
    SHARED_ID_CSS,
    bulk_discard_label,
    bulk_revert_label,
    filter_options,
    run_button_label,
    section_toggle_label,
    select_all_label,
)
from uwazi_admin_agent.drivers.web.run_service import (
    current_pass,
    get_op_status_counts,
    get_property_scores,
    get_run,
    get_suggestion_languages,
)
from uwazi_admin_agent.drivers.web.session import (
    bind_client_instance,
    page_instance_url,
)
from uwazi_admin_agent.drivers.web.task_dialogs import script_dialog

from .core import (
    _STATUS_LABELS,
    _change_table,
    _check_drift_now,
    _discard_from_state,
    _drift_banner,
    _poll_gate_toast,
    _poll_progress,
    _refresh_all,
    _refresh_sort_controls,
    _reset_global_selection,
    _revert_from_state,
    _run_from_state,
    _set_sort,
    _summary_row,
    _toggle_expand_all,
    _toggle_select_all,
    _toggle_sort_direction,
    _update_table_now,
)
from .entity_window import _merged_labels, _merged_types, _template_labels, _template_types


@ui.page("/review/{run_id}")
def review_page(run_id: str, request: Request) -> None:
    """Render the review page for one run (login required).

    The URL keeps the ``instance`` token, so a reload lands on the login page
    of the instance this run belongs to (see
    :func:`uwazi_admin_agent.drivers.links.resolve_page_target`) instead of the
    bare-path guidance.
    """
    instance, redirect = page_target(
        f"/review/{run_id}",
        request.query_params.get("instance", ""),
        request.query_params.get("user", ""),
    )
    if redirect is not None:
        ui.navigate.to(redirect)
        return
    if not instance:  # defensive: a served page always resolved one
        ui.navigate.to("/")
        return
    ui.colors(primary="#2c3e50", secondary="#18bc9c", accent="#f39c12")
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

    # The expansion's styling: Quasar's ``q-table--no-wrap`` turns every
    # descendant ``td`` into nowrap, so the suboperation cells need the wrap
    # restored explicitly; and the panel reads as one card with a colored LEFT
    # border in the operation's color, which is what tells the groups apart once
    # everything is expanded.
    ui.add_head_html(
        f"""
        <style>
        .subs-expand {{ white-space: normal; }}
        .subs-table {{ width: 100%; border-collapse: collapse; table-layout: fixed; white-space: normal; }}
        .subs-table th {{ border-bottom: 1px solid rgba(0,0,0,0.12); }}
        .subs-table td {{ border-bottom: 1px solid rgba(0,0,0,0.06); vertical-align: top; white-space: normal; }}
        .subs-table td > div {{ overflow-wrap: anywhere; }}
        .subs-card {{
            border: 1px solid rgba(0,0,0,0.12);
            border-left: 4px solid var(--subs-border-color, #607d8b);
            border-radius: 4px;
            background: rgba(0,0,0,0.02);
            padding: 4px 8px 8px 8px;
        }}
        body.body--dark .subs-card {{ background: rgba(255,255,255,0.04); border-color: rgba(255,255,255,0.15); }}
        .subs-table td.subs-actions {{ white-space: nowrap; width: 132px; }}
        .subs-table td.subs-actions .q-btn {{ margin: 0; }}
        .subs-context {{ border-left: 3px solid rgba(0,0,0,0.12); padding-left: 8px; }}
        body.body--dark .subs-context {{ border-left-color: rgba(255,255,255,0.15); }}
        .review-no-selectable thead th:first-child {{ visibility: hidden; }}
        {SHARED_ID_CSS}
        </style>
        """
    )

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
        # The collapsible sections' initial state: the FILTERS are ON when the
        # page opens (narrowing the table is the first thing an operator does),
        # the stats stay behind the menu. The menu items and the containers read
        # these flags, so the label always states the next click.
        "_filters_visible": True,
        "_stats_visible": False,
        # The table's always-visible sort. 'recorded' keeps the script's order;
        # 'modified' (descending by default) puts the latest review activity on
        # top. The store orders the WHOLE filtered set, not just this page.
        "sort_by": "recorded",
        "sort_desc": False,
        "loading": False,
        # Set while the page control is rewritten programmatically: assigning a
        # widget value fires its own change handler, and that must not reload
        # the table in a loop.
        "_suppress_page_events": False,
        # The SUBOPERATION selection: the suggestion rows the operator ticked,
        # held as row ids because that is the only identity finer than an
        # (entity, language) pair — "run this one property of this one
        # language". The parent operation's tick is DERIVED from its parts, so
        # the two levels cannot disagree.
        "_checked": set(),
        # Whole-result selection (the button, not the page header checkbox):
        # every SUBOPERATION matching the current filter, across every page.
        # The ids are resolved from the store once, and ``_global_refs`` keeps
        # their identity so Run/Discard/Revert do not need the page payload.
        "_select_all": False,
        "_global_refs": {},
        # Which operations are expanded (the table's ``expanded`` prop is the
        # source of truth, which is what makes "Expand all" possible), and the
        # page-wide MODE ("all"/"none"/"mixed") that a page flip carries over:
        # Expand all then Next must land expanded, Collapse all then Next
        # collapsed.
        "_expanded": set(),
        "_expand_mode": "none",
        # Set while a filter control is reset programmatically: assigning a
        # control's value fires its own change handler, and the reset must not
        # re-enter the filter logic.
        "_suppress_filter_events": False,
    }

    with ui.header().classes("items-center justify-between"):
        with ui.row().classes("items-center"):
            ui.button(icon="arrow_back", on_click=lambda: ui.navigate.to(page_link("/app"))).props("flat color=secondary")
            ui.label(f"Review — {detail.run_id}").classes("text-h6 q-ml-sm")
            state["_status_badge"] = ui.badge(detail.status.value).props("color=grey-7").classes("q-ml-sm")
        with ui.row().classes("items-center"):
            # The controlled instance this run writes to — the same header link
            # the runs table carries, so the operator always knows which instance
            # the rows they are about to write belong to.
            instance_url = page_instance_url()
            if instance_url:
                ui.icon("link", color="secondary").classes("q-mr-xs")
                ui.link(instance_url, instance_url, new_tab=True).classes("text-white")
            ui.button("Back to tasks", on_click=lambda: ui.navigate.to(page_link("/app"))).props("flat color=secondary")

    with ui.column().classes("w-full items-center"):
        with ui.card().classes("w-full max-w-7xl"):
            # Row 1: the hamburger ALONE, at the top right. The menu is the
            # page's own corner control, so it gets a row of its own instead
            # of sharing a row with the actions.
            with ui.row().classes("w-full items-center justify-end q-mb-sm"):
                _header_menu(state, run_id, has_script=bool(detail.script))
            # The two collapsible sections: the stats (the header cards) and
            # the filters. The FILTERS are visible when the page opens — they
            # narrow the table, which is the operator's first job — while the
            # stats start hidden. The menu toggles both in and out. The stats
            # sit ABOVE the filters: the counters describe the whole, the
            # filters narrow it.
            with ui.column().classes("w-full") as stats_section:
                state["_stats_section"] = stats_section
                _summary_row(state)
            with ui.column().classes("w-full") as filters_section:
                state["_filters_section"] = filters_section
                _filter_bar(state)
            # Row 2: the actions, aligned LEFT. They act on the rows the
            # operator ticks, so they sit with the table, on the side the
            # reading starts from.
            with ui.row().classes("w-full items-center gap-2 q-mb-sm"):
                _action_bar(state)
            # The drift verdict sits BELOW the action bar, just above the
            # table: it is the state of the data the table shows, so it reads
            # as a banner over the table, not as one more control above it.
            # It is NOT hidden: a stale table is a safety warning, not a
            # statistic.
            _drift_banner(state)
            _sort_bar(state)
            _change_table(state)
            # Apply the initial visibility the menu labels already advertise:
            # the filters ON, the stats off. Entering the page ALWAYS resets to
            # this — the filters are there to be used, not discovered.
            state["_stats_section"].set_visibility(state["_stats_visible"])
            state["_filters_section"].set_visibility(state["_filters_visible"])

    if current_pass(page_instance_url(), run_id) is None:
        ui.notify(
            "No scan recorded yet — the automatic scan right after generation may still be running",
            type="info",
        )

    ui.timer(3.0, lambda: _poll_progress(state))
    # The operation gate's fast poller: while a Run/Revert/refresh is held, this
    # keeps the live progress toast alive with the live fraction ("Running —
    # 13/1230 suboperations…"); idle, it does nothing.
    ui.timer(0.8, lambda: _poll_gate_toast(state))


def _header_menu(state: dict[str, Any], run_id: str, *, has_script: bool = False) -> None:
    """The page's hamburger menu: every control that is read on demand.

    One menu in the card's top right holds the controls the operator reaches
    for occasionally — the two collapsible sections (stats and filters), the
    drift check, the table refresh, and the run's prompt and script. The page
    itself is the table; the filters open WITH the page, and the rest is one
    click away.

    The section items state what the click WILL do ("Show filters" /
    "Hide filters"), so the label is the item's own contract — the filters
    item opens reading "Hide filters" because the section starts visible.
    """
    # The menu is nested INSIDE the button: Quasar anchors a menu to its
    # parent element, so a sibling menu would hang off the whole toolbar row
    # instead of the button that opens it.
    with ui.button(icon="menu", on_click=lambda: menu.open()).props("flat dense color=secondary"):
        with ui.menu() as menu:
            state["_filters_item"], state["_filters_label"] = _menu_item(
                "filter_alt",
                section_toggle_label("filters", bool(state.get("_filters_visible", True))),
                lambda: _toggle_section(state, "filters"),
            )
            state["_stats_item"], state["_stats_label"] = _menu_item(
                "insights",
                section_toggle_label("stats", bool(state.get("_stats_visible", False))),
                lambda: _toggle_section(state, "stats"),
            )
            ui.separator()
            _menu_item(
                "sync", "Check drift", lambda: background_tasks.create(_check_drift_now(state, client=context.client))
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
                    on_table_changed=lambda: _refresh_all(state),
                    # A regeneration rewrites the table in place. Move to the
                    # tasks window, where the run's row shows `regenerating`
                    # until the new script and table are committed.
                    on_run_started=lambda: ui.navigate.to(page_link("/app")),
                ),
            )
            if has_script:
                # The script item exists only for a run that HAS one — the same
                # rule the tasks table's row menu follows.
                _menu_item("code", "Show script", lambda: script_dialog(run_id))


def _menu_item(icon: str, text: str, on_click: Any) -> tuple[Any, Any]:
    """One menu item: an icon and a label.

    Returns the item and its label: the item can be disabled while its action
    runs, and the label's text is what the section items rewrite when the
    section they toggle changes state.
    """
    with ui.item(on_click=on_click) as item:
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


def _update_table_clicked(state: dict[str, Any]) -> None:
    """Start the Uwazi sync from the menu, with the menu item disabled while it runs."""
    item = state.get("_update_table_item")
    if item is not None and not item.is_deleted:
        item.disable()
    background_tasks.create(_update_table_now(state, client=context.client))


def _filter_bar(state: dict[str, Any]) -> None:
    """The filters, in TWO rows: the search above, the filters below.

    The search gets a row of its own because it is a different kind of
    control: the dropdowns below pick a value out of a known set, while the
    search is free text over the whole table. Splitting them keeps the
    dropdown row from wrapping.

    The whole bar is aligned LEFT, under the stats: the filters narrow what
    the table shows, and the operator reads them from the same side as the
    rows they narrow.
    """
    with ui.column().classes("w-full q-mb-sm"):
        # Row 1: the search on its own.
        with ui.row().classes("w-full items-center gap-2"):
            state["_text_input"] = (
                ui.input(
                    "Search title / sharedId",
                    value=state["text"],
                    on_change=lambda e: _set_filter(state, "text", e.value or ""),
                )
                .props("dense outlined clearable")
                .classes("w-full")
            )
        # Row 2: the filters, with the clear (Reset) button LAST on the right.
        with ui.row().classes("w-full items-center gap-2"):
            # The Property filter comes FIRST: it is the one that decides WHAT
            # the table shows (every property the task writes is an option, and
            # picking one narrows the table to the suboperations writing that
            # property). Each option carries its own accuracy, and "any
            # property" carries the average of the properties that HAVE one.
            scores = get_property_scores(page_instance_url(), state["run_id"])
            labels_by_template = _template_labels(state["run_id"])
            state["_template_labels"] = labels_by_template
            state["_labels"] = _merged_labels(labels_by_template)
            # Types ride along with labels so date values render as calendar
            # dates in the table and in the entity window.
            types_by_template = _template_types(state["run_id"])
            state["_template_types"] = types_by_template
            state["_types"] = _merged_types(types_by_template)
            state["_property_select"] = (
                ui.select(
                    {"": any_property_option(scores), **property_options(scores, state["_labels"])},
                    value=state["property"],
                    label="Property",
                    on_change=lambda e: _set_filter(state, "property", e.value or ""),
                )
                .props("dense outlined")
                .classes("min-w-[240px]")
            )
            # The Language filter names the language a SUBOPERATION is written
            # in: the operation row is one entity whatever languages it writes,
            # so the language has to be selectable below the row. It sits right
            # after Property: both narrow WHAT the table shows.
            state["_language_select"] = (
                ui.select(
                    {
                        "": "any language",
                        **{lang: lang for lang in get_suggestion_languages(page_instance_url(), state["run_id"])},
                    },
                    value=state["language"],
                    label="Language",
                    on_change=lambda e: _set_filter(state, "language", e.value or ""),
                )
                .props("dense outlined")
                .classes("min-w-[150px]")
            )
            # Operation and Status are MULTI-selects: an operator often wants to
            # see two statuses (or two operations) at once. No "any" option — an empty
            # selection is the unfiltered state, and the Reset button clears it.
            # Their options list only the values the run actually has, each with
            # its row count (see _refresh_filter_counts for the live refresh).
            counts = get_op_status_counts(page_instance_url(), state["run_id"])
            state["_op_select"] = (
                ui.select(
                    filter_options(OP_LABELS, counts.get("op", {}), state["op"]),
                    multiple=True,
                    value=state["op"],
                    label="Operation",
                    on_change=lambda e: _set_filter(state, "op", e.value or []),
                )
                .props("dense outlined use-chips clearable")
                .classes("min-w-[190px]")
            )
            state["_status_select"] = (
                ui.select(
                    filter_options(_STATUS_LABELS, counts.get("status", {}), state["status"]),
                    multiple=True,
                    value=state["status"],
                    label="Status",
                    on_change=lambda e: _set_filter(state, "status", e.value or []),
                )
                .props("dense outlined use-chips clearable")
                .classes("min-w-[140px]")
            )
            # The clear button is the LAST item on the right of the filter row:
            # it undoes the whole row, so it reads as the row's own exit.
            ui.button("Reset", icon="restart_alt", on_click=lambda: _reset_filters(state)).props("flat dense").classes(
                "q-ml-auto"
            )


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
    """Clear EVERY filter: the state, and the control that shows it.

    The clear button has to empty the row it sits in, so each control is
    reset to its own "any" value — the single-value dropdowns to "", the
    multi-selects to an empty list, and the search to "". The change events
    those assignments fire are suppressed, so the reset does not re-enter the
    filter logic (and does not refresh the table once per control).
    """
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


def _action_bar(state: dict[str, Any]) -> None:
    """The selection-driven action bar (the checked rows carry the count)."""
    with ui.row().classes("w-full items-center gap-2"):
        state["_run_button"] = ui.button(
            run_button_label(0),
            icon="play_arrow",
            on_click=lambda: _run_from_state(state),
        ).props("dense color=primary")
        state["_bulk_revert_button"] = ui.button(
            bulk_revert_label(0),
            icon="undo",
            on_click=lambda: _revert_from_state(state),
        ).props("dense flat color=indigo")
        # The bulk Discard: marking the checked operations as "will not be
        # executed" one by one is not workable on a table of thousands, so the
        # selection carries it too.
        state["_bulk_discard_button"] = ui.button(
            bulk_discard_label(0),
            icon="block",
            on_click=lambda: _discard_from_state(state),
        ).props("dense flat color=negative")
        # These last two are view controls, not actions on the selection, so a
        # spacer pushes them to the right edge, away from Run/Revert/Discard.
        ui.space()
        state["_select_all_button"] = ui.button(
            select_all_label(0, False),
            icon="select_all",
            on_click=lambda: _toggle_select_all(state),
        ).props("dense flat")
        # The expand toggle changes what the table SHOWS, not what the operator
        # has selected, so it sits at the far right, away from the actions.
        state["_expand_button"] = ui.button(
            "Expand all",
            icon="unfold_more",
            on_click=lambda: _toggle_expand_all(state),
        ).props("dense flat")


def _sort_bar(state: dict[str, Any]) -> None:
    """The always-visible sort control above the review table.

    It lives outside the collapsible filters: changing what the table READS
    (the filters) is optional, but every operator needs to see the order and
    change it — especially "Modified", which brings the last-touched rows to
    the top. The direction toggle names the order it is showing.
    """
    with ui.row().classes("w-full items-center gap-2 q-mb-sm"):
        ui.icon("sort").classes("text-grey-7")
        state["_sort_select"] = (
            ui.select(
                OPERATION_SORT_OPTIONS,
                value=state["sort_by"],
                label="Sort by",
                on_change=lambda e: _set_sort(state, e.value or "recorded"),
            )
            .props("dense outlined")
            .classes("min-w-[180px]")
        )
        state["_sort_direction_button"] = ui.button(
            icon="arrow_upward",
            on_click=lambda: _toggle_sort_direction(state),
        ).props("flat dense")
        _refresh_sort_controls(state)
