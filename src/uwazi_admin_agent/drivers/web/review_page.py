"""The review page: the operations table, its suboperations, and the entity window.

One page per run (``/review/{run_id}``), separate from the runs table because
reviewing is a different job from running: the selection scan produced rows for
the affected entities, the operator checks a subset, and the actions act on the
checked rows — "Run" executes their values with the deterministic apply
heuristic (no LLM at write time), "Revert" undoes them.

The table's row is an OPERATION (one entity — every language it writes), and
expanding it shows the SUBOPERATIONS it is made of: one per property value per
language, one per link for a relationship batch. That keeps an entity in one
place on screen and lets the actions narrow below the (entity, language) pair:
Run/Discard can address a single property, and Revert can put a single property
back to its backup value.

Scale rules this module obeys:

- the table reads ``get_operation_rows`` (SQL GROUP BY + LIMIT/OFFSET in the
  store), with the page's ``Suggestion`` rows attached as suboperations;
- the filter is a SUBOPERATION filter, so an operation whose every part the
  filter removed never reaches the page;
- the header cards come from ``counts`` (SQL ``GROUP BY``) and count
  SUBOPERATIONS — the table's real content — with ONE card for the operations
  themselves (each one says how many parts it is made of);
- the Property dropdown comes FIRST (it decides what the table shows) and its
  options carry each property's accuracy, read from ``property_scores`` (SQL
  ``GROUP BY``) over the whole run; its "any property" option carries the
  average of the properties that HAVE an accuracy;
- selection-driven actions resolve to explicit suggestion-id lists, never a
  Python pass over the whole set.

The **Language** belongs to a SUBOPERATION: Uwazi keeps one entity-language ROW
per translation (a text/richtext property holds a single value on it) while a
select or a date has none, so a translation task records the same entity once
per language and each of those is its own part, checkbox and Run. A change with
no language at all (a select, a title, a delete) shows "—" rather than being
repeated into every language. Running the ``es`` part writes ONLY the Spanish
row: the apply path issues one write per language, narrowed by the selected
row id.

A part is independently checkable only when a Run can execute it alone (a
property value, a link). A delete/publish/merge part is listed with a lock and
the reason: its write is the whole operation, so it runs with the operation's
checkbox.

The operation's **Status** is DERIVED from its parts: "runned" once every part
is written, "discarded" once every part is refused, "pending" while work
remains. The counter beside it reads checked/checkable parts.

The **Target** column carries the open-in-Uwazi link as an icon beside the
(trimmed, ellipsised) title rather than as a column of its own: a plain anchor
opens the entity's public page on the controlled instance in a NEW tab (the
operation's first written language), only for ops whose entity already exists
there — ``update``/``delete`` — and only when ``UWAZI_URL`` is configured; the
URL is built from the row's sharedId with no extra request. Titles are cut in
the row payload as well as in the cell, because a document title is unbounded
and a cell that sizes to its content is what made the table scroll sideways.

Display-only **info** rows reuse that same Target column for arbitrary text the
script surfaced with ``add_info``. They are two-level (the headline plus optional
detail lines) but carry no checkbox, no link, and no action buttons: nothing
about them is written, selected, discarded, or reverted.

The entity window's rows are headed by the property's human LABEL ("Document
Ref"), resolved once per template from ``/api/templates``, with the raw property
name beneath it; nothing is resolved when the instance is unreachable, and the
names are shown exactly as before. Opening the window FROM a suboperation
highlights that property (and still shows the entity's other rows).

Write timing: the selection scan never mutates Uwazi — it runs AUTOMATICALLY
right after generation completes (the selection script cannot call the LLM, so
it needs no approval); only "Accept selected"/Run writes (through the apply
run); Reject only sets a row status. The selection is re-read from the checked
suboperation ids on EVERY action — ``_reload_table`` reassigns ``table.rows``,
so a cached selection goes stale.

Discard: the operator can refuse to execute an operation — "the end user does
not want this" — with the Discard button in the table's Actions cell (and the
bulk Discard over the checked selection). A discard MARKS the selected rows
(DISCARDED, with the reason the operator typed): a Run accepts only PENDING and
REVERTED rows, so a discarded part is never written, and the Undo button brings
it back to the review queue. The rows ALREADY written keep their status (a
discard says what must not be executed; un-writing is the Revert's job). There
is no separate "Discarded" filter: the Status dropdown already reads
"discarded" (the refused parts) and "pending" (what is left to execute), and a
second filter speaking about the same thing would only duplicate it.

Selection count: the action bar's Run button reads ``Run (N)``, where N is the
checked SUBOPERATIONS. Ticking an operation ticks every part of it (locked parts
included, since its Run covers them); un-ticking any part releases the
operation, and ticking the last part ticks it. There are two selection scopes:
the header checkbox selects the visible PAGE (reloading prunes the selection to
those rows), while the "Select all N results" button selects every suboperation
matching the current filter across ALL pages (its ids are resolved from the
store at click time, so page flips never truncate the selection).

Operation gate: Run, Revert, refresh and the app's generation are serialized by
:mod:`uwazi_admin_agent.drivers.web.operation_gate` — ONE Uwazi-write pass at a
time, process-wide. A second attempt is refused with the holder's live count,
and while the gate is held a fast timer keeps one grouped toast on screen
naming the holder by its SUBOPERATION fraction ("Running — 13/1230
suboperations…") until the finishing action replaces it with the outcome.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import quote

from loguru import logger
from nicegui import app, background_tasks, context, ui

from uwazi_admin_agent.configuration import DUMMY_LANGUAGE, REVIEW_PAGE_SIZE
from uwazi_admin_agent.domain.drift import DriftStatus
from uwazi_admin_agent.domain.suggestion import (
    PROPERTY_OPS,
    OperationReviewRow,
    PredictionAttemptStatus,
    Suggestion,
    SuggestionFilter,
    SuggestionStatus,
)
from uwazi_admin_agent.drivers.web.operation_gate import (
    active_text,
    busy_text,
    release,
    set_progress,
    snapshot,
    try_acquire,
)
from uwazi_admin_agent.drivers.web.operations_view import (
    EXPAND_COLUMN,
    STATUS_WITH_COUNT_COLUMN,
    any_property_option,
    entities_of_refs,
    ids_of_refs,
    languages_of_refs,
    operation_row,
    ops_of_refs,
    pairs_of_refs,
    property_options,
    sub_refs,
)
from uwazi_admin_agent.drivers.web.prompt_chat_dialog import prompt_chat_dialog
from uwazi_admin_agent.drivers.web.review_view import (
    ACTIONS_COLUMN,
    DRIFT_STATUS_COLORS,
    MATCH_COLORS,
    OP_COLORS,
    OPS_COLUMN,
    SHARED_ID_CSS,
    STATUS_COLORS,
    TEMPLATE_COLUMN,
    TITLE_COLUMN,
    bulk_discard_label,
    bulk_revert_label,
    cost_card_value,
    drift_banner_text,
    drift_banner_time,
    filter_from_controls,
    full_value,
    match_label,
    property_caption,
    property_heading,
    revert_entity_confirmation,
    run_button_label,
    run_confirmation,
    section_toggle_label,
    select_all_label,
    table_update_label,
    truncate,
)
from uwazi_admin_agent.drivers.web.run_service import (
    apply_outcomes,
    build_backup_store,
    build_suggestion_store,
    check_drift,
    current_pass,
    discard_ids,
    get_drift_state,
    get_entity_segmentation,
    get_entity_suggestions,
    get_operation_rows,
    get_property_scores,
    get_run,
    get_selection_rows,
    get_suggestion_counts,
    get_suggestion_languages,
    get_table_update_time,
    get_templates,
    preflight_selection,
    property_labels_for,
    refresh_baseline_after_run,
    resolve_entity_document,
    restore_ids,
    revert_entities_selection,
    revert_entity_rows,
    revert_suboperation,
    update_table_from_uwazi,
    uwazi_base_url,
)
from uwazi_admin_agent.drivers.web.task_dialogs import script_dialog

# The suboperation statuses a Run may write. ``applied`` is already in Uwazi,
# ``discarded`` was refused (Restore is its undo), and ``accepted``/``edited``
# are waiting for a Run exactly like ``pending``/``reverted``.
_RUNNABLE_STATUSES: frozenset[str] = frozenset(
    {
        SuggestionStatus.PENDING.value,
        SuggestionStatus.ACCEPTED.value,
        SuggestionStatus.EDITED.value,
        SuggestionStatus.REVERTED.value,
    }
)

# Quasar notify's ``group`` key for the operation-progress toast: every
# emission with this key REPLACES the previous one instead of stacking, which
# is what makes one toast read "13/1230 … 47/1230" while the operation runs.
# The completion/failure toasts of the gated actions reuse the key so they
# replace the last live progress fraction instead of leaving it hanging.
GATE_TOAST_GROUP: str = "uwazi-operation"


def _claim_gate(kind: str, run_id: str, client: Any) -> bool:
    """Claim the gate's single Uwazi-write slot, or refuse with a busy toast (plain).

    The gate holds ONE mutating Uwazi operation process-wide (Run, Revert,
    refresh, generation) so no two passes interleave their writes, the store
    updates, or the entity snapshots. Failure notifies why with the holder
    counted live — the operator reads the very fraction that is still moving.
    """
    if try_acquire(kind, run_id):
        return True
    busy = snapshot()
    with client:
        ui.notify(busy_text(busy), type="warning", multi_line=True)
    return False


def _poll_gate_toast(state: dict[str, Any]) -> None:
    """Keep ONE grouped toast alive naming the in-flight Uwazi operation.

    The gate's snapshot is polled at 0.8 s: every change (or every ~2s, to keep
    the toast alive when progress stalls in a long indeterminate phase) reissues
    the grouped notification with the fresh fraction and its filling bar
    (``progress``), so Quasar replaces it in place instead of stacking. A final
    toast from the finishing action reuses the group and replaces this one with
    the outcome; when the gate goes idle the last fraction toast simply expires
    (finite ``timeout``), never lingering on a closed operation.
    """
    active = snapshot()
    if active is None:
        state["_gate_signature"] = None
        return  # nothing running; the last grouped toast expires on its own
    signature = (active.kind, active.run_id, active.done, active.total)
    now = time.monotonic()
    if signature == state.get("_gate_signature") and now - float(state.get("_gate_emitted") or 0.0) < 2.0:
        return
    state["_gate_signature"] = signature
    state["_gate_emitted"] = now
    ui.notify(
        active_text(active),
        type="ongoing",
        group=GATE_TOAST_GROUP,
        progress=(active.done / active.total) if active.total > 0 else True,
        timeout=8_000,
        multi_line=True,
    )


def _modal_header(dialog: Any, title: str) -> None:
    """Modal title row with a top-right Close button (matches app.py's helper)."""
    with ui.row().classes("w-full items-center justify-between no-wrap shrink-0"):
        ui.label(title).classes("text-h6")
        ui.button("Close", icon="close", on_click=dialog.close).props("flat")


def _store() -> Any:
    """The shared suggestion store (SQL-side status writes for row actions)."""
    return build_suggestion_store()


@ui.page("/review/{run_id}")
def review_page(run_id: str) -> None:
    """Render the review page for one run (login required)."""
    if not app.storage.user.get("user"):
        ui.navigate.to("/")
        return
    ui.colors(primary="#2c3e50", secondary="#18bc9c", accent="#f39c12")
    _build_page(run_id)


def _build_page(run_id: str) -> None:
    try:
        detail = get_run(run_id)
    except Exception as exc:  # noqa: BLE001 — a missing run shows an error, never a crash
        with ui.column().classes("w-full items-center justify-center min-h-screen"):
            ui.label(f"Cannot open review for {run_id!r}: {exc}").classes("text-body1 text-negative")
            ui.button("Back to tasks", on_click=lambda: ui.navigate.to("/app")).props("flat")
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
        {SHARED_ID_CSS}
        </style>
        """
    )

    state: dict[str, Any] = {
        "run_id": run_id,
        "offset": 0,
        "total": 0,
        "status": "",
        "op": "",
        "property": "",
        "match": "",
        "language": "",
        "text": "",
        "loading": False,
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
        # source of truth, which is what makes "Expand all" possible).
        "_expanded": set(),
        # Set while a filter control is reset programmatically: assigning a
        # control's value fires its own change handler, and the reset must not
        # re-enter the filter logic.
        "_suppress_filter_events": False,
    }

    with ui.header().classes("items-center justify-between"):
        with ui.row().classes("items-center"):
            ui.button(icon="arrow_back", on_click=lambda: ui.navigate.to("/app")).props("flat color=secondary")
            ui.label(f"Review — {detail.run_id}").classes("text-h6 q-ml-sm")
            ui.badge(detail.status.value).props("color=grey-7").classes("q-ml-sm")
        with ui.row().classes("items-center"):
            # The controlled instance this run writes to — the same header link
            # the runs table carries, so the operator always knows which instance
            # the rows they are about to write belong to.
            instance_url = uwazi_base_url()
            if instance_url:
                ui.icon("link", color="secondary").classes("q-mr-xs")
                ui.link(instance_url, instance_url, new_tab=True).classes("text-white")
            ui.button("Back to tasks", on_click=lambda: ui.navigate.to("/app")).props("flat color=secondary")

    with ui.column().classes("w-full items-center"):
        with ui.card().classes("w-full max-w-7xl"):
            # Row 1: the hamburger ALONE, at the top right. The menu is the
            # page's own corner control, so it gets a row of its own instead
            # of sharing a row with the actions.
            with ui.row().classes("w-full items-center justify-end q-mb-sm"):
                _header_menu(state, run_id, has_script=bool(detail.script))
            # The two collapsible sections: the stats (the header cards) and
            # the filters. Both are hidden by default — the table is the page's
            # content, and the counters and the narrowing controls are read
            # when the operator wants them. The menu is the only way in and
            # out of them. The stats sit ABOVE the filters: the counters
            # describe the whole, the filters narrow it.
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
            _change_table(state)
            # Both sections start HIDDEN: the table is the page's content, and
            # the stats and the filters are read on demand.
            state["_stats_section"].set_visibility(False)
            state["_filters_section"].set_visibility(False)

    if current_pass(run_id) is None:
        ui.notify(
            "No scan recorded yet — the automatic scan right after generation may still be running",
            type="info",
        )

    ui.timer(3.0, lambda: _poll_progress(state))
    # The operation gate's fast poller: while a Run/Revert/refresh is held, this
    # keeps the grouped progress toast alive with the live fraction ("Running —
    # 13/1230 suboperations…"); idle, it does nothing.
    ui.timer(0.8, lambda: _poll_gate_toast(state))


# --- collapsible sections ----------------------------------------------------


def _header_menu(state: dict[str, Any], run_id: str, *, has_script: bool = False) -> None:
    """The page's hamburger menu: every control that is read on demand.

    One menu in the card's top right holds the controls the operator reaches
    for occasionally — the two collapsible sections (stats and filters), the
    drift check, the table refresh, and the run's prompt and script. The page
    itself is the table; everything else is one click away.

    The section items state what the click WILL do ("Show filters" /
    "Hide filters"), so the label is the item's own contract.
    """
    # The menu is nested INSIDE the button: Quasar anchors a menu to its
    # parent element, so a sibling menu would hang off the whole toolbar row
    # instead of the button that opens it.
    with ui.button(icon="menu", on_click=lambda: menu.open()).props("flat dense color=secondary"):
        with ui.menu() as menu:
            state["_filters_item"], state["_filters_label"] = _menu_item(
                "filter_alt",
                section_toggle_label("filters", False),
                lambda: _toggle_section(state, "filters"),
            )
            state["_stats_item"], state["_stats_label"] = _menu_item(
                "insights",
                section_toggle_label("stats", False),
                lambda: _toggle_section(state, "stats"),
            )
            ui.separator()
            _menu_item(
                "sync", "Check drift", lambda: background_tasks.create(_check_drift_now(state, client=context.client))
            )
            state["_update_table_item"], state["_update_table_menu_label"] = _menu_item(
                "cloud_sync", "Update table", lambda: _update_table_clicked(state)
            )
            ui.separator()
            _menu_item(
                "description",
                "Show prompt",
                lambda: prompt_chat_dialog(run_id, on_table_changed=lambda: _refresh_all(state)),
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
    """Start the table refresh from the menu, with the menu item disabled while it runs."""
    item = state.get("_update_table_item")
    if item is not None and not item.is_deleted:
        item.disable()
    background_tasks.create(_update_table_now(state, client=context.client))


# --- header ------------------------------------------------------------------


def _summary_row(state: dict[str, Any]) -> None:
    holder = ui.row().classes("w-full items-center gap-3 q-mb-sm")
    state["_summary"] = holder
    _render_summary(state)


def _render_summary(state: dict[str, Any]) -> None:
    holder = state.get("_summary")
    if holder is None or holder.is_deleted:
        return
    counts = get_suggestion_counts(state["run_id"])
    pass_row = current_pass(state["run_id"])
    holder.clear()
    with holder:
        # ONE card for the operations and their suboperations: the Operations
        # card counts the PARTS (the table's real content — one proposal per
        # property/language/link), and the
        # entities behind them ride underneath, so the operator reads the
        # table's two levels without a second card.
        _card(
            "Operations",
            f"{counts.get('subs_total', 0):,}",
            caption=f"{counts.get('operations_total', 0):,} entities",
        )
        _card("Pending", f"{counts.get('subs_pending', 0):,}", "grey")
        _card("Discarded", f"{counts.get('subs_discarded', 0):,}", "red")
        # Applied reads as a fraction so the operator sees how much of the task
        # is written without counting rows: applied suboperations / all of them.
        _card("Applied", f"{counts.get('subs_applied', 0):,} / {counts.get('subs_total', 0):,}", "blue")
        _card("Reverted", f"{counts.get('subs_reverted', 0):,}", "indigo")
        if pass_row is not None:
            # The LLM cost card: the spend against the cap, as money. It sits
            # at the FAR RIGHT of the row,
            # apart from the counters: the cost is not a count of the table's
            # content, it is the pass's own money, and it gets its own space.
            _card(
                "LLM cost",
                cost_card_value(
                    pass_row.budget.call_cost(pass_row.spent.input_tokens, pass_row.spent.output_tokens),
                    pass_row.budget.max_cost,
                ),
                "teal",
                classes="q-ml-auto",
            )


def _card(label: str, value: str, color: str = "primary", caption: str = "", classes: str = "") -> None:
    """One header card: a label, the figure, and an optional second line.

    The caption carries the SECOND reading of a figure that has two (the
    Operations card counts the parts, and the entities behind them ride
    underneath), so the two levels of the table are read from one card.

    ``classes`` positions the card within the row (the LLM cost card pushes
    itself to the right with ``q-ml-auto``).
    """
    with ui.card().classes(f"q-pa-sm {classes}").style("min-width: 118px"):
        ui.label(label).classes("text-caption text-grey-7")
        ui.label(value).classes(f"text-subtitle1 text-bold text-{color}")
        if caption:
            ui.label(caption).classes("text-caption text-grey-6")


# --- filters -----------------------------------------------------------------


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
            scores = get_property_scores(state["run_id"])
            labels_by_template = _template_labels(state["run_id"])
            state["_template_labels"] = labels_by_template
            state["_labels"] = _merged_labels(labels_by_template)
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
            state["_op_select"] = (
                ui.select(
                    {"": "any op", **{op: op for op in OP_COLORS}},
                    value=state["op"],
                    label="Op",
                    on_change=lambda e: _set_filter(state, "op", e.value or ""),
                )
                .props("dense outlined")
                .classes("min-w-[150px]")
            )
            state["_status_select"] = (
                ui.select(
                    {"": "any status", **{s: s for s in STATUS_COLORS}},
                    value=state["status"],
                    label="Status",
                    on_change=lambda e: _set_filter(state, "status", e.value or ""),
                )
                .props("dense outlined")
                .classes("min-w-[140px]")
            )
            # The Language filter names the language a SUBOPERATION is written
            # in: the operation row is one entity whatever languages it writes,
            # so the language has to be selectable below the row.
            state["_language_select"] = (
                ui.select(
                    {"": "any language", **{lang: lang for lang in get_suggestion_languages(state["run_id"])}},
                    value=state["language"],
                    label="Language",
                    on_change=lambda e: _set_filter(state, "language", e.value or ""),
                )
                .props("dense outlined")
                .classes("min-w-[150px]")
            )
            state["_match_select"] = (
                ui.select(
                    {"": "any match", "exact": "exact", "mismatch": "mismatch", "new": "new"},
                    value=state["match"],
                    label="Match",
                    on_change=lambda e: _set_filter(state, "match", e.value or ""),
                )
                .props("dense outlined")
                .classes("min-w-[130px]")
            )
            ui.button("Mismatches only", icon="rule", on_click=lambda: _set_filter(state, "match", "mismatch")).props(
                "flat dense"
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
    reset to its own "any" value — the dropdowns to "", the search to "". The
    change events those assignments fire are suppressed, so the reset does not
    re-enter the filter logic (and does not refresh the table once per
    control).
    """
    state.update(
        {
            "status": "",
            "op": "",
            "property": "",
            "match": "",
            "language": "",
            "text": "",
            "offset": 0,
        }
    )
    widgets = [state.get(f"_{key}_select") for key in ("property", "op", "status", "language", "match")]
    widgets.append(state.get("_text_input"))
    state["_suppress_filter_events"] = True
    try:
        for widget in widgets:
            if widget is not None and not widget.is_deleted:
                widget.value = ""
    finally:
        state["_suppress_filter_events"] = False
    _reset_global_selection(state)
    _refresh_all(state)


def _current_filter(state: dict[str, Any], *, accepted_only: bool = False) -> SuggestionFilter:
    return filter_from_controls(
        status=state["status"],
        op=state["op"],
        property_name=state["property"],
        match=state["match"],
        language=state["language"],
        text=state["text"],
        offset=state["offset"],
        limit=REVIEW_PAGE_SIZE,
        accepted_only=accepted_only,
    )


# --- actions -----------------------------------------------------------------


def _row_of_key(state: dict[str, Any], row_key: Any) -> dict[str, Any] | None:
    """The table payload for one operation key, or None (pure lookup)."""
    table = state.get("_table")
    if table is None or table.is_deleted:
        return None
    for row in table.rows or []:
        if isinstance(row, dict) and str(row.get("row_key")) == str(row_key):
            return row
    return None


def _row_refs(row: dict[str, Any] | None) -> list[tuple[int, str, str | None, str]]:
    """The suboperation refs of one table row payload (pure)."""
    return sub_refs((row or {}).get("subs") or [])


def _checked_refs(state: dict[str, Any]) -> list[tuple[int, str, str | None, str]]:
    """The refs of every checked SUBOPERATION, page or whole-result (pure over state).

    The checked set is ids, so the identity is looked up in the page payload and,
    in whole-result mode, in the projection cached when the mode was entered —
    which is what lets a Run spanning pages resolve each row's entity, language
    and op without loading the set again.
    """
    ids = state.get("_checked") or set()
    refs: dict[int, tuple[int, str, str | None, str]] = {}
    table = state.get("_table")
    if table is not None and not table.is_deleted:
        for row in table.rows or []:
            if not isinstance(row, dict):
                continue
            for ref in _row_refs(row):
                refs[ref[0]] = ref
    refs.update(state.get("_global_refs") or {})
    return [refs[sub_id] for sub_id in ids if sub_id in refs]


def _selection_snapshot(
    state: dict[str, Any],
) -> tuple[list[tuple[int, str, str | None, str]], list[str]]:
    """What Run would act on right now: the checked suboperations and their ops."""
    refs = _checked_refs(state)
    return refs, ops_of_refs(refs)


def _run_from_state(state: dict[str, Any]) -> None:
    """Open the Run confirmation for the selection the button's count describes."""
    refs, ops = _selection_snapshot(state)
    _run_refs(state, refs, ops)


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


def _revert_from_state(state: dict[str, Any]) -> None:
    """Open the bulk-Revert confirmation for the selection the button's count describes."""
    refs, _ops = _selection_snapshot(state)
    if not refs:
        ui.notify("Nothing selected", type="warning")
        return
    entities = entities_of_refs(refs)

    def _confirm() -> None:
        # The client is captured HERE, inside the click's slot context (the same
        # pattern _run_rows uses): the background task starts with an empty slot
        # stack, so every UI call it makes must re-enter the client explicitly.
        client = context.client
        background_tasks.create(_revert_selected(state, entities, client), name=f"revert {state['run_id']}")

    title = f"Revert {len(entities)} entit{'' if len(entities) == 1 else 'ies'}"
    message = (
        f"Restore {len(entities)} entit{'' if len(entities) == 1 else 'ies'} to the backup the run made "
        "BEFORE any of its writes. ALL languages of each entity are restored (they share one backup); "
        "only the checked entities are reverted."
    )
    _simple_confirm(title, message, _confirm)


async def _revert_selected(state: dict[str, Any], entities: list[str], client: Any) -> None:
    """Revert the selected entities (one restore each), then refresh the page."""
    user = app.storage.user.get("user")
    password = app.storage.user.get("password")
    if not user or not password:
        with client:
            ui.notify("Not logged in to Uwazi", type="negative")
        return
    # One Uwazi-write operation at a time (the gate), as for the Run.
    if not _claim_gate("revert", state["run_id"], client):
        return
    try:
        try:
            outcome = await revert_entities_selection(
                state["run_id"],
                [(sid, None) for sid in entities],
                user,
                password,
                progress=set_progress,
            )
            with client:
                ui.notify(
                    f"Revert done — {outcome.summary()}",
                    type="positive" if not outcome.failures else "warning",
                    multi_line=True,
                    group=GATE_TOAST_GROUP,
                )
        except Exception as exc:  # noqa: BLE001 — the toast reports; the page never crashes
            logger.exception("bulk revert failed")
            with client:
                ui.notify(
                    f"Revert failed: {exc}",
                    type="negative",
                    multi_line=True,
                    group=GATE_TOAST_GROUP,
                )
    finally:
        release("revert", state["run_id"])
        with client:
            _clear_checked(state)
            _refresh_all(state)


# --- discard ------------------------------------------------------------------


def _discard_from_state(state: dict[str, Any]) -> None:
    """Open the bulk-Discard dialog for the checked SUBOPERATIONS.

    The dialog asks for the reason too: the operator discards an operation
    because the end user does not want it executed, and WHY is worth keeping
    with the rows (it is stored as the rows' discard reason).
    """
    refs, _ops = _selection_snapshot(state)
    ids = ids_of_refs(refs)
    if not ids:
        ui.notify("Nothing selected", type="warning")
        return

    def _submit(reason: str | None) -> None:
        # The client is captured HERE, inside the click's slot context (the same
        # pattern _run_rows uses): the background task starts with an empty slot
        # stack, so every UI call it makes must re-enter the client explicitly.
        client = context.client
        background_tasks.create(
            _discard_ids(state, ids, reason, client),
            name=f"discard {state['run_id']}",
        )

    noun = "suboperation" if len(ids) == 1 else "suboperations"
    _reason_dialog(
        f"Mark {len(ids)} checked {noun} as discarded. They will NOT be executed by a Run, "
        "and the reason is stored with them.",
        _submit,
        title=f"Discard {len(ids)} {noun}",
        confirm_label="Discard",
        reason_label="Reason (optional — why the end user does not want these executed)",
    )


def _discard_op_clicked(state: dict[str, Any], row_key: str) -> None:
    """Mark ONE operation as discarded (its visible suboperations).

    The filter is a SUBOPERATION filter, so "the operation" here means the
    parts the table is showing: discarding an operation under a property filter
    refuses those property writes, not the ones the filter hid.
    """
    row = _row_of_key(state, row_key)
    if row is None or row.get("is_info"):
        return
    ids = ids_of_refs(_row_refs(row))
    if not ids:
        return

    def _submit(reason: str | None) -> None:
        client = context.client
        background_tasks.create(_discard_ids(state, ids, reason, client), name=f"discard {row_key}")

    _reason_dialog(
        "Mark this operation as discarded? It will NOT be executed by a Run. The reason is stored with it.",
        _submit,
        title="Discard operation",
        confirm_label="Discard",
        reason_label="Reason (optional — why the end user does not want this executed)",
    )


async def _discard_ids(
    state: dict[str, Any],
    ids: list[int],
    reason: str | None,
    client: Any,
) -> None:
    """Mark the given SUBOPERATIONS as discarded, then refresh the page."""
    try:
        count = await asyncio.to_thread(discard_ids, state["run_id"], ids, reason)
        with client:
            ui.notify(
                f"Discarded {count} suboperation(s) — a Run will not execute them",
                type="positive",
                multi_line=True,
            )
    except Exception as exc:  # noqa: BLE001 — the toast reports; the page never crashes
        logger.exception("discard failed")
        with client:
            ui.notify(f"Discard failed: {exc}", type="negative", multi_line=True)
    finally:
        with client:
            _clear_checked(state)
            _refresh_all(state)


def _restore_op_clicked(state: dict[str, Any], row_key: str) -> None:
    """Undo the discard of ONE operation (its visible rows go back to pending)."""
    row = _row_of_key(state, row_key)
    if row is None or row.get("is_info"):
        return
    ids = ids_of_refs(_row_refs(row))
    client = context.client
    background_tasks.create(_restore_ids(state, ids, client), name=f"restore {row_key}")


async def _restore_ids(state: dict[str, Any], ids: list[int], client: Any) -> None:
    """Return the given SUBOPERATIONS' discarded rows to pending, then refresh."""
    try:
        count = await asyncio.to_thread(restore_ids, state["run_id"], ids)
        with client:
            ui.notify(f"Restored {count} suboperation(s) — they can be run again", type="positive")
    except Exception as exc:  # noqa: BLE001 — the toast reports; the page never crashes
        logger.exception("restore failed")
        with client:
            ui.notify(f"Restore failed: {exc}", type="negative", multi_line=True)
    finally:
        with client:
            _clear_checked(state)
            _refresh_all(state)


# --- per-row revert ----------------------------------------------------------


def _backup_dates_for(rows: list[Any], run_id: str) -> dict[str, Any]:
    """``sharedId -> backup captured_at`` for the page's entities (local files only).

    One read attempt per entity (best-effort): an absent snapshot file means
    the run never wrote to that entity, so no tooltip date. Errors are logged,
    never raised — a missing date must not break the table render.
    """
    dates: dict[str, Any] = {}
    store = build_backup_store()
    for item in rows:
        try:
            snapshot = store.load_snapshot(run_id, item.shared_id)
        except FileNotFoundError:
            continue
        except Exception as exc:  # noqa: BLE001 — a broken file degrades to no tooltip
            logger.warning("could not load backup for tooltip shared_id={}: {}", item.shared_id, exc)
            continue
        dates[item.shared_id] = snapshot.captured_at
    return dates


async def _revert_entity_clicked(state: dict[str, Any], shared_id: str) -> None:
    """Revert one entity (all its languages) to its single backup, then refresh.

    Confirm first: the restore overwrites the entity's rows in every language
    the task touched, not just the clicked suboperation's language.
    """
    tooltip = state.get("_backup_dates", {}).get(shared_id)
    tooltip_text = tooltip.strftime("%Y-%m-%d %H:%M") if tooltip is not None else ""

    def _confirm() -> None:
        client = context.client
        background_tasks.create(_revert_entity_rows(state, shared_id, client), name=f"revert {shared_id}")

    title, message = revert_entity_confirmation(shared_id, tooltip_text)
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
    user = app.storage.user.get("user")
    password = app.storage.user.get("password")
    if not user or not password:
        with client:
            ui.notify("Not logged in to Uwazi", type="negative")
        return
    # One Uwazi-write operation at a time (the gate), as for the Run.
    if not _claim_gate("revert", state["run_id"], client):
        return
    try:
        try:
            row_language = await revert_suboperation(
                state["run_id"], shared_id, language, property_name, suggestion_id, user, password
            )
            with client:
                ui.notify(
                    f"Reverted {property_name} ({row_language}) to its backup value",
                    type="positive",
                    group=GATE_TOAST_GROUP,
                )
        except Exception as exc:  # noqa: BLE001 — the toast reports; the page never crashes
            logger.exception("suboperation revert failed")
            with client:
                ui.notify(
                    f"Revert failed: {exc}",
                    type="negative",
                    multi_line=True,
                    group=GATE_TOAST_GROUP,
                )
    finally:
        release("revert", state["run_id"])
        with client:
            _clear_checked(state)
            _refresh_all(state)


async def _revert_entity_rows(state: dict[str, Any], shared_id: str, client: Any) -> None:
    user = app.storage.user.get("user")
    password = app.storage.user.get("password")
    if not user or not password:
        with client:
            ui.notify("Not logged in to Uwazi", type="negative")
        return
    # One Uwazi-write operation at a time (the gate), as for the Run.
    if not _claim_gate("revert", state["run_id"], client):
        return
    set_progress(0, 1)
    try:
        try:
            info = await revert_entity_rows(state["run_id"], shared_id, user, password)
            set_progress(1, 1)
            with client:
                ui.notify(
                    f"Reverted {shared_id} to its backup"
                    + (f" ({info.backup_created_at:%Y-%m-%d %H:%M})" if info.backup_created_at else ""),
                    type="positive",
                    group=GATE_TOAST_GROUP,
                )
        except Exception as exc:  # noqa: BLE001 — the toast reports, the page never crashes
            logger.exception("per-entity revert failed")
            with client:
                ui.notify(
                    f"Revert failed: {exc}",
                    type="negative",
                    multi_line=True,
                    group=GATE_TOAST_GROUP,
                )
    finally:
        release("revert", state["run_id"])
        with client:
            _clear_checked(state)
            _refresh_all(state)


# --- drift banner ------------------------------------------------------------


def _drift_banner(state: dict[str, Any]) -> None:
    """The drift line: what the last drift check concluded.

    It sits BELOW the action bar, just above the table — the state of the data
    the table shows. Renders the STORED verdict (or "not checked yet"); the
    open-page auto-check and the manual button update it. One row: an icon +
    the text + (right) the check-drift and update-table buttons.
    """
    with ui.row().classes("w-full items-center gap-2 q-mb-sm"):
        state["_drift_icon"] = ui.icon("sync", color="grey-6").classes("text-body1")
        state["_drift_label"] = ui.label(drift_banner_text(status="unknown", detail=None)).classes(
            "text-body2 text-grey-7 grow"
        )
        state["_table_update_label"] = ui.label(table_update_label(get_table_update_time(state["run_id"]))).classes(
            "text-caption text-grey-6"
        )

    # The verdict's text is stored per run: apply it immediately, then let the
    # open-page auto-check refresh it (a fresh page should not show a stale
    # 'valid' while Uwazi moved — the check replaces the text in place).
    stored = get_drift_state(state["run_id"])
    _apply_drift_state(state, stored, checking=stored is None)
    if stored is None or stored.status is DriftStatus.UNKNOWN or stored.checked_by == "sync":
        background_tasks.create(_check_drift_now(state, notify=False, client=context.client))
    elif stored.status is not DriftStatus.VALID:
        # A previous page visit already found drift; re-check in the background
        # so the operator sees CURRENT drift rather than yesterday's.
        background_tasks.create(_check_drift_now(state, notify=False, client=context.client))


async def _update_table_now(state: dict[str, Any], client: Any) -> None:
    """Refresh the table in place, then repaint the label, drift badge and grid."""
    user = app.storage.user.get("user")
    password = app.storage.user.get("password")
    if not user or not password:
        with client:
            ui.notify("Not logged in to Uwazi", type="negative")
        return
    item = state.get("_update_table_item")
    label = state.get("_table_update_label")
    # The refresh is a heavy Uwazi pass (a full script scan): it goes through
    # the same gate as a Run, so it can neither overlap one nor be overlapped
    # by one.
    if not _claim_gate("refresh", state["run_id"], client):
        return
    with client:
        if item is not None and not item.is_deleted:
            item.disable()
        if label is not None and not label.is_deleted:
            label.text = "Updating table…"
    try:
        result = await update_table_from_uwazi(state["run_id"], user, password)
        updated = get_drift_state(state["run_id"])
        with client:
            _apply_drift_state(state, updated)
            if label is not None and not label.is_deleted:
                label.text = table_update_label(get_table_update_time(state["run_id"]))
            ui.notify(result.message(), type="positive", multi_line=True, group=GATE_TOAST_GROUP)
            _refresh_all(state)
    except Exception as exc:  # noqa: BLE001 — the page reports, never crashes
        logger.exception("table update failed")
        with client:
            ui.notify(
                f"Table update failed: {exc}",
                type="negative",
                multi_line=True,
                group=GATE_TOAST_GROUP,
            )
    finally:
        release("refresh", state["run_id"])
        if item is not None and not item.is_deleted:
            with client:
                item.enable()


def _apply_drift_state(state: dict[str, Any], stored: Any, *, checking: bool = False) -> None:
    """Paint one verdict (or the checking glyph) onto the banner elements."""
    label = state.get("_drift_label")
    icon = state.get("_drift_icon")
    if label is None or label.is_deleted:
        return
    if checking:
        label.text = drift_banner_text(status="unknown", detail=None, checking=True)
        if icon is not None and not icon.is_deleted:
            icon.name = "sync"
            icon.classes("animate-spin text-body1")
            icon.color = "blue"
            icon.update()
        return
    if stored is None:
        label.text = drift_banner_text(status="unknown", detail=None)
        return
    label.text = drift_banner_text(
        status=stored.status.value,
        detail=stored.detail,
    ) + drift_banner_time(stored.checked_at)
    color = DRIFT_STATUS_COLORS.get(stored.status.value, "grey-6")
    icon_name = {
        "unknown": "help_outline",
        "valid": "check_circle",
        "drifted": "warning",
        "error": "error",
    }.get(stored.status.value, "help_outline")
    if icon is not None and not icon.is_deleted:
        icon.name = icon_name
        # ``classes(...)`` ADDS by default: without removing the checking
        # stage's ``animate-spin`` here, the verdict icon would keep spinning
        # forever. A fresh class list also drops stale text-color entries so
        # the new verdict's color wins.
        icon.classes(replace="text-body1")
        icon.classes(f"text-{color}")
        icon.update()


async def _check_drift_now(state: dict[str, Any], notify: bool = True, client: Any = None) -> None:
    """Run the drift check in the background thread and repaint the banner.

    ``client`` is captured by the caller INSIDE the page's slot context (the
    same pattern ``_run_rows`` uses): a background task starts with an empty
    slot stack, so every UI call it makes — the banner updates and the
    toasts — must re-enter the client explicitly via ``with client:``.
    """
    user = app.storage.user.get("user")
    password = app.storage.user.get("password")
    if not user or not password:
        _apply_drift_state(
            state,
            None,
        )
        state["_drift_label"].text = "Drift check failed — not logged in to Uwazi"
        return
    _apply_drift_state(state, None, checking=True)
    try:
        # ``check_drift`` is SYNCHRONOUS (it blocks on Uwazi HTTP calls), so it
        # must run in a worker thread, like every other service call (the
        # apply_outcomes/revert_run pattern) — awaiting it directly raised
        # "object DriftStateRow can't be used in 'await' expression".
        updated = await asyncio.to_thread(
            check_drift,
            state["run_id"],
            user,
            password,
            "auto" if not notify else "manual",
        )
    except Exception as exc:  # noqa: BLE001 — the banner reports; the page never crashes
        logger.exception("drift check failed")
        state["_drift_label"].text = f"Drift check failed — {exc}"
        return
    _apply_drift_state(state, updated)
    if not notify or client is None:
        return
    try:
        with client:
            if updated.status.value == "drifted":
                ui.notify(updated.detail or "Drift detected in Uwazi", type="warning")
            elif updated.status.value == "valid":
                ui.notify("No drift detected — table is current", type="positive")
    except RuntimeError as exc:  # noqa: BLE001 — a disconnected client cannot show a toast
        logger.debug("could not notify drift verdict (client gone): {}", exc)


def _apply_checked(state: dict[str, Any], ids: list[int], checked: bool) -> None:
    """Add/remove SUBOPERATION ids from the checked set (in place)."""
    target: set[int] = state.setdefault("_checked", set())
    if checked:
        target.update(ids)
    else:
        target.difference_update(ids)


def _sync_selection(state: dict[str, Any]) -> None:
    """Reflect the checked SUBOPERATION ids onto the table's rows and selection.

    The checked set is the source of truth: each part's checkbox reads it, and
    the parent operation's checkbox is DERIVED from it — an operation is ticked
    exactly when every one of its parts is: ticking the last part ticks the
    operation too, and un-ticking any part releases it.

    A LOCKED part has no checkbox but still belongs to the selection its
    operation's tick created (a delete/publish/merge part runs with the
    operation), so the parent's derivation counts every part, while the counter
    beside the status badge counts only the checkable ones.
    """
    table = state.get("_table")
    if table is None or table.is_deleted:
        return
    checked: set[int] = state.get("_checked") or set()
    selected: list[dict[str, Any]] = []
    for row in table.rows or []:
        if not isinstance(row, dict):
            continue
        if row.get("is_info"):
            # Display-only rows are never part of a selection.
            continue
        subs = row.get("subs") or []
        for sub in subs:
            if isinstance(sub, dict):
                sub["checked"] = bool(sub.get("selectable")) and sub.get("id") in checked
        if subs and all(sub.get("id") in checked for sub in subs if isinstance(sub, dict)):
            selected.append(row)
    table.selected = selected
    table.update()
    _refresh_selection_counts(state)


def _clear_checked(state: dict[str, Any]) -> None:
    """Forget the whole selection after an action wrote or moved its rows.

    The rows an action addressed changed status (or left the filter), so keeping
    them ticked would invite a second Run on work that is already written and
    leave the count describing stale ids. The next ``_reload_table`` re-derives
    the table from the store, so clearing here is enough.
    """
    state["_checked"] = set()
    state["_select_all"] = False
    state["_global_refs"] = {}


def _clear_selection(state: dict[str, Any], *, clear_page: bool) -> None:
    """Leave whole-result mode and (optionally) uncheck the visible page."""
    state["_select_all"] = False
    state["_global_refs"] = {}
    if clear_page:
        state["_checked"] = set()
    _sync_selection(state)


def _reset_global_selection(state: dict[str, Any]) -> None:
    """Drop a whole-result selection when the filter that defined it changes."""
    if state.get("_select_all"):
        _clear_selection(state, clear_page=True)


def _toggle_select_all(state: dict[str, Any]) -> None:
    """Select every SUBOPERATION matching the filter across pages, or clear it."""
    if state.get("_select_all"):
        _clear_selection(state, clear_page=True)
        return
    if int(state.get("total") or 0) <= 0:
        return
    # The projection is a row-id list, not a page: selection_rows ignores
    # limit/offset, so this covers every operation the filter matches.
    refs = get_selection_rows(state["run_id"], _current_filter(state))
    state["_global_refs"] = {ref[0]: ref for ref in refs}
    state["_checked"] = {ref[0] for ref in refs}
    state["_select_all"] = True
    _sync_selection(state)


def _toggle_sub(state: dict[str, Any], args: Any) -> None:
    """Apply one suboperation's checkbox (row_key, sub id, checked).

    The tick SELECTS a part for the run; it never moves the part between the
    statuses — only Run and Discard do that.
    """
    if not isinstance(args, list) or len(args) != 3:
        return
    row_key, sub_id, checked = args
    row = _row_of_key(state, row_key)
    if row is None:
        return
    sub = next(
        (one for one in (row.get("subs") or []) if isinstance(one, dict) and one.get("id") == sub_id),
        None,
    )
    if sub is None or not sub.get("selectable"):
        return
    _apply_checked(state, [int(sub_id)], bool(checked))
    _sync_selection(state)


def _set_children_checked(state: dict[str, Any], row: dict[str, Any], checked: bool) -> None:
    """Tick/untick every part of one operation.

    A tick is a SELECTION, not an acceptance: the parts' statuses are left
    alone. Locked parts are included because the operation's Run covers them —
    they simply have no checkbox of their own to show it. Info rows are
    display-only and carry no selection at all.
    """
    if row.get("is_info"):
        return
    ids = [int(sub["id"]) for sub in (row.get("subs") or []) if isinstance(sub, dict) and isinstance(sub.get("id"), int)]
    _apply_checked(state, ids, checked)


def _on_table_select(state: dict[str, Any], event: Any) -> None:
    """The table's selection callback: only the counters need refreshing.

    The cascade to the suboperations lives in :func:`_selection_changed`, which
    listens to the raw event carrying WHICH rows were added or removed.
    """
    _refresh_selection_counts(state)


def _selection_changed(state: dict[str, Any], event: Any) -> None:
    """The raw ``selection`` event: propagate an operation's tick to its parts."""
    args = getattr(event, "args", None)
    if not isinstance(args, dict):
        return
    added = bool(args.get("added"))
    for row in args.get("rows") or []:
        if not isinstance(row, dict):
            continue
        payload = _row_of_key(state, row.get("row_key"))
        if payload is not None:
            _set_children_checked(state, payload, added)
    _sync_selection(state)


def _run_refs(
    state: dict[str, Any],
    refs: list[tuple[int, str, str | None, str]],
    ops: list[str] | None = None,
) -> None:
    """Execute the given SUBOPERATIONS with the Run heuristic (revertable).

    The Run action IS the acceptance: ``ApplySuggestionsUseCase.apply`` accepts
    the selected rows' PENDING state as it runs them. The selection carries both
    the entity-language PAIRS (how the apply path addresses a row) and the
    suggestion IDS (how it narrows below the pair), so running one property of a
    translation row writes exactly that property.
    """
    ids = ids_of_refs(refs)
    if not ids:
        ui.notify("Nothing selected", type="warning")
        return
    pairs = pairs_of_refs(refs)
    languages = languages_of_refs(refs)
    resolved_ops = ops or ops_of_refs(refs)

    def _confirm() -> None:
        # The client is captured HERE, inside the click's slot context: the
        # background task starts with an empty slot stack, so every UI call it
        # makes (notify, refresh) must re-enter the client explicitly.
        client = context.client
        background_tasks.create(
            _run_rows(state, pairs, ids, client),
            name=f"run {state['run_id']}",
        )

    title, message = run_confirmation(len(ids), resolved_ops, languages)
    _simple_confirm(title, message, _confirm)


def _run_op_clicked(state: dict[str, Any], row_key: str) -> None:
    """The operation's Run button: run the parts the operation covers.

    The parts the operator ticked (plus the locked parts, which run with the
    operation) are written; when NOTHING checkable is ticked, the button means
    the whole operation — the only reading that makes a row-level Run useful.
    """
    row = _row_of_key(state, row_key)
    if row is None:
        return
    if row.get("is_info"):
        ui.notify("Info rows are display-only", type="info")
        return
    subs = [sub for sub in (row.get("subs") or []) if isinstance(sub, dict)]
    checked = [sub for sub in subs if sub.get("selectable") and sub.get("checked")]
    if checked:
        # The ticked parts PLUS the locked ones: a locked part (a merge's source
        # deletes, a delete's teardown) is part of the operation and has no
        # checkbox to be ticked on its own.
        covered = [sub for sub in subs if not sub.get("selectable") or sub.get("checked")]
    else:
        # Nothing checkable is ticked: the row-level Run means the whole
        # operation — the only reading that makes the button useful.
        covered = subs
    if not covered:
        ui.notify("This operation has nothing to run", type="warning")
        return
    # A part that was already written (or refused) is not written again by the
    # operation's Run: "run this operation" means the work that remains, and a
    # discard says NOT to execute it (Restore is its undo).
    covered = [sub for sub in covered if _runnable(sub)]
    if not covered:
        ui.notify("Nothing left to run in this operation", type="warning")
        return
    _set_children_checked(state, row, True)
    _sync_selection(state)
    _run_refs(state, sub_refs(covered))


def _runnable(sub: dict[str, Any]) -> bool:
    """Whether a suboperation payload is still waiting to be written (pure)."""
    return str(sub.get("status") or "") in _RUNNABLE_STATUSES


def _run_sub_clicked(state: dict[str, Any], args: Any) -> None:
    """The suboperation's Run button: write that one part."""
    if not isinstance(args, list) or len(args) != 2:
        return
    row_key, sub_id = args
    row = _row_of_key(state, row_key)
    if row is None:
        return
    sub = next(
        (one for one in (row.get("subs") or []) if isinstance(one, dict) and one.get("id") == sub_id),
        None,
    )
    if sub is None or sub.get("is_info"):
        return
    if not _runnable(sub):
        ui.notify(
            f"This suboperation is already {sub.get('status')} — nothing to run"
            + (" (Restore it first)" if sub.get("status") == "discarded" else ""),
            type="warning",
        )
        return
    refs = [ref for ref in _row_refs(row) if ref[0] == sub_id]
    if not refs:
        return
    _apply_checked(state, [int(sub_id)], True)
    _sync_selection(state)
    _run_refs(state, refs)


async def _run_rows(
    state: dict[str, Any],
    pairs: list[tuple[str, str | None]],
    ids: list[int],
    client: Any,
) -> None:
    user = app.storage.user.get("user")
    password = app.storage.user.get("password")
    if not user or not password:
        with client:
            ui.notify("Not logged in to Uwazi", type="negative")
        return
    # One Uwazi-write operation at a time (the gate): a concurrent Run, Revert
    # or refresh — this page's or any other's — is refused with its live count.
    if not _claim_gate("run", state["run_id"], client):
        return
    try:
        try:
            preflight = await asyncio.to_thread(
                preflight_selection,
                state["run_id"],
                ids,
                user,
                password,
            )
        except Exception as exc:  # noqa: BLE001 — an unverifiable selection must not run
            logger.exception("run preflight failed")
            with client:
                ui.notify(f"Could not verify the selection against Uwazi: {exc}", type="negative", multi_line=True)
            return
        if not preflight.ok:
            with client:
                ui.notify(preflight.message(), type="warning", multi_line=True)
            return
        try:
            await apply_outcomes(state["run_id"], user, password, pairs, ids, progress=set_progress)
            # The writes bumped Uwazi editDates: re-sync the baseline so the run's
            # own writes are the new reference, not drift.
            refresh_baseline_after_run(state["run_id"], user, password)
            with client:
                ui.notify(
                    f"Executed — 'Revert' on run {state['run_id']!r} (or Revert task) undoes it",
                    type="positive",
                    multi_line=True,
                    group=GATE_TOAST_GROUP,
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception("run (heuristic apply) failed")
            with client:
                ui.notify(
                    f"Run failed: {exc}",
                    type="negative",
                    multi_line=True,
                    group=GATE_TOAST_GROUP,
                )
    finally:
        release("run", state["run_id"])
        with client:
            _clear_checked(state)
            _refresh_all(state)


# --- entity table ------------------------------------------------------------


def _change_table(state: dict[str, Any]) -> None:
    """The OPERATIONS table: one row per affected entity, each with suboperations.

    The row is the entity (every language it writes), and expanding it shows the
    SUBOPERATIONS it is made of: one per property value per language, one per
    link for a relationship batch. That is what makes a translation task one row
    with two language parts instead of two peer rows, and what lets Run/Discard/
    Revert address a single property.

    The body slot renders both levels so ``props.expand`` (Quasar's expansion,
    keyed by ``row_key`` so it survives a rows refresh) can show the panel, and
    ``props.selected`` keeps the header checkbox, the row checkbox and the Run
    count in step. Colors and labels arrive on the row (computed in Python): a
    Vue slot cannot reach ``window.*``.
    """
    columns = [
        {"name": "expand", "label": "", "field": "expand", "align": "center", "sortable": False, "style": EXPAND_COLUMN},
        {"name": "title", "label": "Target", "field": "title", "align": "left", "sortable": False, "style": TITLE_COLUMN},
        {
            "name": "template",
            "label": "Template",
            "field": "template",
            "align": "left",
            "sortable": False,
            "style": TEMPLATE_COLUMN,
        },
        {"name": "ops", "label": "Ops", "field": "ops", "align": "left", "sortable": False, "style": OPS_COLUMN},
        {
            "name": "run_status",
            "label": "Status",
            "field": "run_status",
            "align": "left",
            "sortable": False,
            "style": STATUS_WITH_COUNT_COLUMN,
        },
        {
            "name": "actions",
            "label": "Actions",
            "field": "actions",
            "align": "center",
            "sortable": False,
            "style": ACTIONS_COLUMN,
        },
    ]
    table = ui.table(
        rows=[],
        columns=columns,
        # The operation's key is the ENTITY: one row however many languages and
        # properties its suboperations write.
        row_key="row_key",
        selection="multiple",
        pagination={"rowsPerPage": 0},
        on_select=lambda e: _on_table_select(state, e),
    ).classes("w-full")
    # The table's expansion state lives in the ``expanded`` prop, so the server
    # owns it: that is what lets the page expand (and collapse) every operation
    # at once. It starts empty.
    table.props["expanded"] = []

    table.add_slot(
        "body",
        """
        <q-tr :props="props">
          <q-td auto-width>
            <q-checkbox v-if="!props.row.is_info" v-model="props.selected" dense />
          </q-td>
          <q-td key="expand" :props="props" class="text-center">
            <q-btn v-if="!props.row.is_info || props.row.has_subs" dense flat size="sm"
                   @click.stop="props.expand = !props.expand"
                   :icon="props.expand ? 'expand_less' : 'chevron_right'"
                   :title="props.expand ? 'Collapse the suboperations' : 'Show the suboperations'" />
          </q-td>
          <q-td key="title" :props="props">
            <div v-if="props.row.is_info" class="text-body2"
                 style="white-space: pre-wrap; overflow-wrap: anywhere; user-select: text;">{{ props.row.title_full }}</div>
            <div v-else class="row items-center no-wrap" style="width: 100%;">
              <span class="text-grey-7 q-mr-xs review-shared-id"
                    style="user-select: text;">{{ props.row.shared_id }}</span>
              <a v-if="props.row.url" :href="props.row.url" target="_blank" rel="noopener" class="q-mr-xs"
                 style="flex: 0 0 auto;" title="Open this entity in Uwazi" draggable="false">
                <q-icon name="open_in_new" size="15px" color="primary" /></a>
              <span class="text-primary" style="flex: 1 1 auto; min-width: 0; overflow: hidden;
                    text-overflow: ellipsis; white-space: nowrap; cursor: pointer; user-select: text;"
                    :title="props.row.title_full"
                    @click="$event => { const sel = $event.target.ownerDocument.getSelection();
                      if (sel && !sel.isCollapsed && sel.rangeCount &&
                      ($event.target.contains(sel.getRangeAt(0).startContainer) ||
                      $event.target.contains(sel.getRangeAt(0).endContainer))) return;
                      $parent.$emit('open_entity', props.row); }">{{ props.row.title }}</span>
            </div>
          </q-td>
          <q-td key="template" :props="props">{{ props.row.template || '—' }}</q-td>
          <q-td key="ops" :props="props">
            <q-badge v-for="(op, i) in props.row.ops.split(', ')" :key="op"
                     :color="props.row.op_colors[i]" class="q-mr-xs">{{ op }}</q-badge>
          </q-td>
          <q-td key="run_status" :props="props" class="text-no-wrap">
            <q-badge :color="props.row.run_status_color" class="q-mr-xs">{{ props.row.run_status }}</q-badge>
            <span v-if="props.row.has_validated" :class="'text-' + props.row.validated_color"
                  class="text-caption text-bold">{{ props.row.validated }}</span>
          </q-td>
          <q-td key="actions" :props="props" class="text-no-wrap">
            <template v-if="!props.row.is_info">
              <q-btn dense flat icon="play_arrow" color="primary"
                     title="Run this operation: the ticked suboperations (all of them when none is ticked)"
                     @click.stop="$parent.$emit('run_op', props.row)" />
              <q-btn v-if="props.row.revertable" dense flat icon="undo" color="indigo"
                     :title="props.row.revert_tooltip || 'Revert this entity to its backup (all its languages)'"
                     @click.stop="$parent.$emit('revert_op', props.row)" />
              <q-btn v-if="props.row.run_status === 'discarded'" dense flat icon="restore" color="orange"
                     title="Undo the discard — these suboperations go back to the review queue"
                     @click.stop="$parent.$emit('restore_op', props.row)" />
              <q-btn v-else-if="props.row.run_status !== 'runned'" dense flat icon="block" color="negative"
                     title="Mark this operation's visible suboperations as discarded — a Run will not execute them"
                     @click.stop="$parent.$emit('discard_op', props.row)" />
              <q-btn dense flat icon="edit" color="teal"
                     title="Open the entity window (all its rows, every language)"
                     @click.stop="$parent.$emit('open_entity', props.row)" />
            </template>
          </q-td>
        </q-tr>
        <tr v-show="props.expand">
          <td colspan="100%" class="q-pa-sm subs-expand">
            <div class="subs-card" :style="{ '--subs-border-color': props.row.border_color }">
              <div style="width: 0; min-width: 100%; overflow-x: auto;">
                <table class="subs-table">
                  <colgroup>
                    <col style="width: 40px;" />
                    <col style="width: 22%;" />
                    <col style="width: 76px;" />
                    <col style="width: 19%;" />
                    <col style="width: 22%;" />
                    <col style="width: 84px;" />
                    <col style="width: 104px;" />
                    <col style="width: 132px;" />
                  </colgroup>
                  <thead>
                    <tr class="text-caption text-grey-7">
                      <th></th>
                      <th class="text-left q-pa-xs">Change</th>
                      <th class="text-left q-pa-xs">Language</th>
                      <th class="text-left q-pa-xs">Current value</th>
                      <th class="text-left q-pa-xs">Proposed value</th>
                      <th class="text-left q-pa-xs">Match</th>
                      <th class="text-left q-pa-xs">Status</th>
                      <th class="text-left q-pa-xs">Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    <template v-for="sub in props.row.subs">
                      <tr v-if="sub.is_info" :key="'info-' + sub.id">
                        <td></td>
                        <td class="q-pa-xs" colspan="7">
                          <div class="text-body2"
                               style="white-space: pre-wrap; overflow-wrap: anywhere;
                                      user-select: text;">{{ sub.label }}</div>
                        </td>
                      </tr>
                      <tr v-else :key="sub.id">
                        <td class="text-center">
                          <q-checkbox v-if="sub.selectable" dense :model-value="sub.checked"
                                      @update:model-value="v => $parent.$emit('toggle_sub', props.row.row_key, sub.id, v)" />
                          <q-icon v-else name="lock" size="16px" color="grey-5"
                                  :title="sub.note || 'not independently runnable'" />
                        </td>
                        <td class="q-pa-xs">
                          <div class="text-body2">{{ sub.label }}</div>
                          <div v-if="sub.caption" class="text-caption text-grey-6">{{ sub.caption }}</div>
                        </td>
                        <td class="q-pa-xs">
                          <q-badge v-if="sub.language" color="teal-9">{{ sub.language }}</q-badge>
                          <span v-else class="text-grey-6">—</span>
                        </td>
                        <td class="q-pa-xs"><div class="text-body2 text-grey-7">{{ sub.current }}</div></td>
                        <td class="q-pa-xs"><div class="text-body2">{{ sub.proposed }}</div></td>
                        <td class="q-pa-xs"><q-badge :color="sub.match_color">{{ sub.match }}</q-badge></td>
                        <td class="q-pa-xs"><q-badge :color="sub.status_color">{{ sub.status }}</q-badge></td>
                        <td class="q-pa-xs subs-actions">
                          <template v-if="sub.selectable">
                            <q-btn dense flat icon="play_arrow" color="primary"
                                   title="Run this suboperation (only this property)"
                                   @click.stop="$parent.$emit('run_sub', props.row.row_key, sub.id)" />
                            <q-btn v-if="sub.status === 'discarded'" dense flat icon="restore" color="orange"
                                   title="Undo the discard — this suboperation goes back to the review queue"
                                   @click.stop="$parent.$emit('restore_sub', props.row.row_key, sub.id)" />
                            <q-btn v-else-if="sub.status !== 'runned'" dense flat icon="block" color="negative"
                                   title="Discard this suboperation — a Run will not execute it"
                                   @click.stop="$parent.$emit('discard_sub', props.row.row_key, sub.id)" />
                            <q-btn v-if="sub.revertable" dense flat icon="undo" color="indigo"
                                   title="Revert this one property to its backup value"
                                   @click.stop="$parent.$emit('revert_sub', props.row.row_key, sub.id)" />
                          </template>
                          <q-btn dense flat icon="edit" color="teal"
                                 title="Open the entity window at this language"
                                 @click.stop="$parent.$emit('edit_sub', props.row.row_key, sub.id)" />
                        </td>
                      </tr>
                    </template>
                  </tbody>
                </table>
              </div>
            </div>
          </td>
        </tr>
        """,
    )
    table.on("run_op", lambda e: _run_op_clicked(state, _row_key_of(getattr(e, "args", None))))
    table.on("discard_op", lambda e: _discard_op_clicked(state, _row_key_of(getattr(e, "args", None))))
    table.on("restore_op", lambda e: _restore_op_clicked(state, _row_key_of(getattr(e, "args", None))))
    table.on("revert_op", lambda e: _revert_entity_clicked(state, _row_key_of(getattr(e, "args", None))))
    table.on("open_entity", lambda e: _open_entity_row(state, _row_of(e)))
    table.on("toggle_sub", lambda e: _toggle_sub(state, getattr(e, "args", None)))
    table.on("run_sub", lambda e: _run_sub_clicked(state, getattr(e, "args", None)))
    table.on("discard_sub", lambda e: _discard_sub_clicked(state, getattr(e, "args", None)))
    table.on("restore_sub", lambda e: _restore_sub_clicked(state, getattr(e, "args", None)))
    table.on("revert_sub", lambda e: _revert_sub_clicked(state, getattr(e, "args", None)))
    table.on("edit_sub", lambda e: _edit_sub_clicked(state, getattr(e, "args", None)))
    # The RAW selection event carries WHICH rows were added/removed, which is
    # what the cascade to the suboperations needs (``on_select`` only carries
    # the selection itself).
    table.on("selection", lambda e: _selection_changed(state, e), ["added", "rows", "keys"])
    # A chevron changed the expansion: mirror it in the prop so "Expand all"
    # knows what is already open.
    table.on("update:expanded", lambda e: _expanded_changed(state, e))
    state["_table"] = table

    with ui.row().classes("w-full items-center justify-between"):
        state["_page_label"] = ui.label("").classes("text-body2 text-grey-7")
        with ui.row().classes("items-center"):
            ui.button(icon="chevron_left", on_click=lambda: _page(state, -1)).props("flat dense")
            state["_page_number"] = ui.label("").classes("text-body2 q-mx-sm")
            ui.button(icon="chevron_right", on_click=lambda: _page(state, 1)).props("flat dense")
    _reload_table(state)


def _row_of(event: Any) -> dict[str, Any]:
    """The clicked row dict out of a table event (NiceGUI sends it as ``args``)."""
    args = event.args
    return args if isinstance(args, dict) else {}


def _row_key_of(args: Any) -> str:
    """The row_key out of a table event's args (a dict for row emits, a list for sub emits)."""
    if isinstance(args, dict):
        return str(args.get("row_key") or "")
    if isinstance(args, list) and args:
        return str(args[0])
    return ""


def _sub_of(state: dict[str, Any], args: Any) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """The (operation payload, suboperation payload) a suboperation event addresses."""
    if not isinstance(args, list) or len(args) != 2:
        return None, None
    row = _row_of_key(state, args[0])
    if row is None:
        return None, None
    sub = next(
        (one for one in (row.get("subs") or []) if isinstance(one, dict) and one.get("id") == args[1]),
        None,
    )
    return row, sub


def _open_entity_row(state: dict[str, Any], row: dict[str, Any]) -> None:
    """Open the entity window for one operation row (its first written language)."""
    if row.get("is_info"):
        return
    shared_id = str(row.get("shared_id") or "")
    languages = [lang for lang in (row.get("languages") or []) if lang]
    _open_entity_window(state, shared_id, languages[0] if languages else None)


def _discard_sub_clicked(state: dict[str, Any], args: Any) -> None:
    """Discard ONE suboperation (its id is the whole selection)."""
    _row, sub = _sub_of(state, args)
    if sub is None or sub.get("is_info") or not isinstance(sub.get("id"), int):
        return
    sub_id = int(sub["id"])
    client = context.client
    background_tasks.create(
        _discard_ids(state, [sub_id], "discarded from the suboperations table", client),
        name=f"discard sub {sub_id}",
    )


def _restore_sub_clicked(state: dict[str, Any], args: Any) -> None:
    """Undo ONE suboperation's discard."""
    _row, sub = _sub_of(state, args)
    if sub is None or sub.get("is_info") or not isinstance(sub.get("id"), int):
        return
    client = context.client
    background_tasks.create(_restore_ids(state, [int(sub["id"])], client), name=f"restore sub {sub['id']}")


def _revert_sub_clicked(state: dict[str, Any], args: Any) -> None:
    """Revert ONE property's write (the suboperation's own undo)."""
    _row, sub = _sub_of(state, args)
    if sub is None or sub.get("is_info") or not sub.get("revertable") or not isinstance(sub.get("id"), int):
        return
    _revert_suboperation_clicked(
        state,
        str(sub.get("shared_id") or ""),
        sub.get("lang"),
        str(sub.get("property_name") or ""),
        int(sub["id"]),
        str(sub.get("label") or "this property"),
    )


def _edit_sub_clicked(state: dict[str, Any], args: Any) -> None:
    """Open the entity window AT the suboperation's language, focused on its property."""
    _row, sub = _sub_of(state, args)
    if sub is None or sub.get("is_info"):
        return
    _open_entity_window(
        state,
        str(sub.get("shared_id") or ""),
        sub.get("lang"),
        focus_property=sub.get("property_name"),
    )


def _toggle_expand_all(state: dict[str, Any]) -> None:
    """Expand every operation — or collapse them all when they all are expanded."""
    table = state.get("_table")
    if table is None or table.is_deleted:
        return
    rows = table.rows or []
    keys = [str(row["row_key"]) for row in rows if isinstance(row, dict)]
    expanded = set(state.get("_expanded") or set())
    if rows and all(key in expanded for key in keys):
        expanded = set()
    else:
        expanded = set(keys)
    table.props["expanded"] = list(expanded)
    state["_expanded"] = expanded
    table.update()
    _refresh_expand_button(state)


def _expanded_changed(state: dict[str, Any], event: Any) -> None:
    """A chevron changed the expansion: mirror it back into the table's prop.

    Quasar's QTable is CONTROLLED by ``expanded``: with the prop set, a click
    only emits ``update:expanded`` and leaves the table's state alone, so the
    server has to write the new list back — without that the chevron does
    nothing.
    """
    args = getattr(event, "args", None)
    keys = args if isinstance(args, list) else []
    state["_expanded"] = {str(key) for key in keys}
    table = state.get("_table")
    if table is not None and not table.is_deleted:
        table.props["expanded"] = list(state["_expanded"])
        table.update()
    _refresh_expand_button(state)


def _refresh_expand_button(state: dict[str, Any]) -> None:
    """The button offers the opposite of the current state: expand or collapse."""
    button = state.get("_expand_button")
    if button is None or button.is_deleted:
        return
    table = state.get("_table")
    rows = (table.rows if table is not None and not table.is_deleted else []) or []
    expanded = state.get("_expanded") or set()
    all_expanded = bool(rows) and all(isinstance(row, dict) and str(row["row_key"]) in expanded for row in rows)
    button.text = "Collapse all" if all_expanded else "Expand all"
    button.set_icon("unfold_less" if all_expanded else "unfold_more")


def _page(state: dict[str, Any], direction: int) -> None:
    new_offset = state["offset"] + direction * REVIEW_PAGE_SIZE
    if new_offset < 0 or new_offset >= state["total"]:
        return
    state["offset"] = new_offset
    _reload_table(state)


def _reload_table(state: dict[str, Any]) -> None:
    """Load one page of OPERATIONS with their suboperations and update the table.

    The filter is a SUBOPERATION filter, so an operation whose every part the
    filter removed never reaches the page — "no suboperations left means no
    row", enforced by the store. The rows carry each entity's public Uwazi URL
    (read once here, so the table costs no extra request for it).
    """
    if state.get("loading"):
        return
    state["loading"] = True
    try:
        rows, total = get_operation_rows(state["run_id"], _current_filter(state))
        state["total"] = total
        base_url = uwazi_base_url()
        # Per-row Revert tooltip: each page reads its entities' backup dates (a
        # local file read per entity — no HTTP). Entities without a backup
        # (never written) carry None and their Revert tooltip falls back to the
        # generic text.
        tooltips = _backup_dates_for(rows, state["run_id"])
        state["_backup_dates"] = tooltips
        labels_by_template = state.get("_template_labels") or {}
        payloads = [
            operation_row(
                item,
                checked_ids=state.get("_checked") or (),
                uwazi_url=base_url,
                labels=_labels_for_operation(item, labels_by_template, state.get("_labels") or {}),
            )
            for item in rows
        ]
        # The Revert icon names the backup it would restore, the way the
        # entity table's did: the date is already read for the confirm dialog.
        for payload in payloads:
            captured = tooltips.get(payload["shared_id"])
            payload["revert_tooltip"] = f"entity backup creation {captured:%Y-%m-%d %H:%M}" if captured is not None else ""
        table = state.get("_table")
        if table is not None and not table.is_deleted:
            table.rows = payloads
            if not state.get("_select_all"):
                # Page selection: drop checked ids that are no longer visible,
                # so the count (and Run) describe what the operator can see.
                visible = {
                    int(sub["id"])
                    for payload in payloads
                    for sub in payload.get("subs") or []
                    if isinstance(sub.get("id"), int)
                }
                state["_checked"] = {sub_id for sub_id in (state.get("_checked") or ()) if sub_id in visible}
            _sync_selection(state)
        label = state.get("_page_label")
        if label is not None and not label.is_deleted:
            first = state["offset"] + 1 if total else 0
            last = min(state["offset"] + REVIEW_PAGE_SIZE, total)
            label.text = f"Showing {first:,}–{last:,} of {total:,} operations"
        page_number = state.get("_page_number")
        if page_number is not None and not page_number.is_deleted:
            pages = max((total + REVIEW_PAGE_SIZE - 1) // REVIEW_PAGE_SIZE, 1)
            page_number.text = f"page {state['offset'] // REVIEW_PAGE_SIZE + 1} / {pages}"
        _refresh_expand_button(state)
    finally:
        state["loading"] = False


def _refresh_selection_counts(state: dict[str, Any]) -> None:
    """Update the selection-driven buttons' texts from the checked SUBOPERATIONS.

    The Run/Discard counts are the checked parts (a count of proposals, not of
    entities); Revert counts the distinct entities those parts belong to,
    because one entity is restored once however many of its rows are ticked.
    """
    refs = _checked_refs(state)
    count = len(refs)
    entities = len(entities_of_refs(refs))
    run_button = state.get("_run_button")
    if run_button is not None and not run_button.is_deleted:
        run_button.text = run_button_label(count)
        run_button.set_enabled(count > 0)
    revert_button = state.get("_bulk_revert_button")
    if revert_button is not None and not revert_button.is_deleted:
        revert_button.text = bulk_revert_label(entities)
        revert_button.set_enabled(entities > 0)
    discard_button = state.get("_bulk_discard_button")
    if discard_button is not None and not discard_button.is_deleted:
        discard_button.text = bulk_discard_label(count)
        discard_button.set_enabled(count > 0)
    select_button = state.get("_select_all_button")
    if select_button is not None and not select_button.is_deleted:
        total = int(state.get("total") or 0)
        all_selected = bool(state.get("_select_all"))
        select_button.text = select_all_label(total, all_selected)
        select_button.set_enabled(all_selected or total > 0)


def _refresh_all(state: dict[str, Any]) -> None:
    """Re-render the summary, the selection buttons, and the current page."""
    try:
        _render_summary(state)
        if state["offset"] >= state["total"] and state["offset"] > 0:
            state["offset"] = max(state["total"] - REVIEW_PAGE_SIZE, 0)
        _reload_table(state)
    except Exception:  # noqa: BLE001 — a refresh failure must not break the page
        logger.exception("review refresh failed")


def _poll_progress(state: dict[str, Any]) -> None:
    """Refresh the header while a pass is RUNNING (progress + spend)."""
    pass_row = current_pass(state["run_id"])
    if pass_row is None or pass_row.status != PredictionAttemptStatus.RUNNING:
        return
    _render_summary(state)
    _reload_table(state)


# --- row actions -------------------------------------------------------------


# --- dialogs -----------------------------------------------------------------


# --- dialogs -----------------------------------------------------------------


def _simple_confirm(title: str, message: str, on_confirm: Any) -> None:
    with context.client.layout:
        with ui.dialog() as dialog, ui.card().classes("w-full max-w-lg"):
            ui.label(title).classes("text-h6")
            ui.label(message).classes("text-body1")
            with ui.row().classes("w-full justify-end"):
                ui.button("Cancel", on_click=dialog.close).props("color=grey-7 flat")
                ui.button("Confirm", color="negative", on_click=lambda: _fire(dialog, on_confirm))
    dialog.open()


def _fire(dialog: Any, on_confirm: Any) -> None:
    dialog.close()
    on_confirm()


def _reason_dialog(
    message: str,
    on_submit: Any,
    *,
    title: str = "",
    confirm_label: str = "Discard",
    reason_label: str = "Reason (optional — used to improve the prompt)",
) -> None:
    """Confirm dialog with an optional reason (the reason is stored with the rows).

    The reason is not decoration: the operator discards an operation because
    the end user does not want it executed, and WHY is kept with the rows so the
    decision can be read back later.
    """
    with context.client.layout:
        with ui.dialog() as dialog, ui.card().classes("w-full max-w-lg"):
            if title:
                ui.label(title).classes("text-h6")
            ui.label(message).classes("text-body1")
            reason_input = ui.textarea(reason_label).classes("w-full")
            with ui.row().classes("w-full justify-end"):
                ui.button("Cancel", on_click=dialog.close).props("color=grey-7 flat")
                ui.button(
                    confirm_label,
                    color="negative",
                    on_click=lambda: _submit_reason(dialog, reason_input, on_submit),
                )
    dialog.open()


def _submit_reason(dialog: Any, reason_input: Any, on_submit: Any) -> None:
    reason = (reason_input.value or "").strip() or None
    dialog.close()
    on_submit(reason)


# --- entity window -----------------------------------------------------------


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

    ``focus_property`` is set when the window was opened FROM a suboperation:
    the clicked property's card is highlighted, while every other property of
    the entity-language group is still shown (the window is the place to see
    the whole row, not just the filtered part).
    """
    _close_entity_window(state)
    suggestions = get_entity_suggestions(state["run_id"], shared_id, language)
    if not suggestions:
        ui.notify(f"No suggestions for {shared_id}", type="warning")
        return
    title = suggestions[0].entity_title or shared_id
    label = f"{title} — {shared_id}" + (f" · {language}" if language else "")
    labels = _property_labels(suggestions)
    # A discarded operation says so in its own window too: the operator who
    # opened it needs to know a Run will not execute its rows.
    discarded = all(s.status is SuggestionStatus.DISCARDED for s in suggestions)
    with context.client.layout:
        with ui.dialog() as dialog, ui.card().classes("w-full").props("style=height:100dvh"):
            dialog.props("maximized")
            with ui.column().classes("w-full h-full items-stretch no-wrap"):
                _entity_header(
                    state,
                    dialog,
                    label,
                    shared_id,
                    [s.op for s in suggestions],
                    language,
                    discarded,
                    suggestions,
                )
                with ui.row().classes("w-full grow no-wrap items-stretch").style("min-height: 0"):
                    document_pane = ui.column().classes("grow").style("min-width: 50%; height: 100%")
                    with ui.column().classes("grow").style("min-width: 45%; height: 100%; overflow-y: auto"):
                        _entity_suggestions(state, suggestions, labels, focus_property=focus_property)
                with ui.row().classes("w-full justify-between shrink-0"):
                    with ui.row().classes("items-center"):
                        ui.button(
                            "Previous row",
                            icon="chevron_left",
                            on_click=lambda: _navigate_entity(state, shared_id, language, -1),
                        ).props("flat dense")
                        ui.button(
                            "Next row",
                            icon="chevron_right",
                            on_click=lambda: _navigate_entity(state, shared_id, language, 1),
                        ).props("flat dense")
                    ui.button("Close", icon="close", on_click=lambda: _close_entity_window(state)).props("flat")
    state["_entity_dialog"] = dialog
    dialog.open()
    background_tasks.create(_load_document(state, shared_id, language, document_pane), name=f"document {shared_id}")


def _close_entity_window(state: dict[str, Any]) -> None:
    """Close the entity window if one is open (the one-window guard)."""
    dialog = state.get("_entity_dialog")
    if dialog is not None and not dialog.is_deleted:
        dialog.close()
    state["_entity_dialog"] = None


async def _load_document(state: dict[str, Any], shared_id: str, language: str | None, pane: Any) -> None:
    """Fill the left pane with the entity's PDF viewer, or its document text.

    The decision is made BEFORE rendering: an entity with no uploaded primary
    document gets the segmentation text panel instead of a viewer that would show
    nothing (the plan's no-blank-viewer rule). Both paths degrade to a message
    rather than an empty box.

    The document is resolved in the ROW's language with a fallback to the
    instance's other configured languages (an entity usually holds its PDF in
    the language it was uploaded in, not the one being translated), and the
    viewer is pointed at a fully-built server URL — the viewer never has to
    reconstruct the entity's identity from query params.
    """
    user = app.storage.user.get("user")
    password = app.storage.user.get("password")
    if pane.is_deleted:
        return
    with pane:
        ui.linear_progress(show_value=False).classes("w-full")
        ui.label("Loading the entity's document…").classes("text-body2 text-grey-7")
    if not user or not password:
        _render_pane_message(pane, "Not logged in to Uwazi — cannot show the document.")
        return
    try:
        document = await resolve_entity_document(user, password, shared_id, language)
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
    await _render_segmentation_fallback(pane, user, password, shared_id, language)


def _viewer_src(state: dict[str, Any], shared_id: str, language: str) -> str:
    """The iframe URL that renders an entity's PDF (pure).

    The document route is passed whole through the viewer's ``src`` parameter,
    URL-encoded: an inner ``&`` would otherwise split into the OUTER query
    string and the viewer would fetch an entity-less URL.
    """
    document_url = (
        f"/review-pdf?run={quote(str(state['run_id']), safe='')}"
        f"&sid={quote(shared_id, safe='')}"
        f"&lang={quote(language, safe='')}"
    )
    return f"/static/pdfjs/review_viewer.html?src={quote(document_url, safe='')}"


async def _render_segmentation_fallback(
    pane: Any, user: str, password: str, shared_id: str, language: str | None = None
) -> None:
    """Show the entity's document TEXT when there is no uploaded primary document."""
    try:
        segmentation = await get_entity_segmentation(user, password, shared_id, language or DUMMY_LANGUAGE)
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


def _entity_header(
    state: dict[str, Any],
    dialog: Any,
    label: str,
    shared_id: str,
    ops: list[str] | None = None,
    language: str | None = None,
    discarded: bool = False,
    suggestions: list[Suggestion] | None = None,
) -> None:
    """The window's title row: Run this entity-language row, or Close."""
    refs = [(s.id, s.shared_id, s.language, s.op) for s in (suggestions or []) if s.id is not None]
    with ui.row().classes("w-full items-center justify-between no-wrap shrink-0"):
        ui.label(label).classes("text-h6")
        with ui.row().classes("items-center no-wrap"):
            if discarded:
                ui.badge("discarded").props("color=negative").classes("q-mr-sm")
            ui.button(
                "Run this row",
                icon="play_arrow",
                on_click=lambda: _run_refs(state, refs, ops),
            ).props("flat dense color=primary")
            ui.button("Close", icon="close", on_click=lambda: _close_entity_window(state)).props("flat dense")


def _template_labels(run_id: str) -> dict[str, dict[str, str]]:
    """``{template_name: {property_name: label}}`` for every template the run writes.

    The review table's child rows and the Property filter show a property's
    human LABEL ("Document Ref"), not the template's raw name
    (``document_ref``). Labels belong to a template, so they are resolved per
    template and cached behind :func:`property_labels_for`; an unreachable
    instance yields ``{}`` and the page falls back to the raw names.
    """
    user = app.storage.user.get("user") or ""
    password = app.storage.user.get("password") or ""
    return {name: property_labels_for(name, user, password) for name in get_templates(run_id)}


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
    return property_labels_for(template_name, app.storage.user.get("user") or "", app.storage.user.get("password") or "")


def _entity_value_label(text: str, classes: str) -> None:
    """One value line in the entity window, rendered IN FULL (wrapped, newlines kept).

    The review table truncates to keep cells on one line; the detail window must
    not — a ten-line paragraph has to be readable there. ``pre-wrap`` preserves
    the value's own line breaks and ``overflow-wrap`` stops a long word from
    widening the pane.
    """
    ui.label(text).classes(classes).style("white-space: pre-wrap; overflow-wrap: anywhere; word-break: break-word")


def _entity_suggestions(
    state: dict[str, Any],
    suggestions: list[Suggestion],
    labels: dict[str, str] | None = None,
    focus_property: str | None = None,
) -> None:
    """The right-hand panel: the precomputed rows the Run heuristic will write.

    Read-only view (see the row's op, property, current and proposed values,
    and the operator's edit when one was saved via the inline editor); the
    commit happens through the header's Run button, not per-row buttons.

    Each card is headed by the property's LABEL — what Uwazi calls it and what
    the operator knows it by — with the raw property name beneath it, so a row is
    both readable and still matchable against the template or a CSV
    (:func:`property_heading` / :func:`property_caption`). ``labels`` is the
    template's label map; without it the heading falls back to the raw name.

    ``focus_property`` highlights the property the window was opened from (a
    suboperation's Edit icon) without hiding the entity's other rows: the
    window is the place to see the whole row.
    """
    resolved = labels or {}
    shown_merge_sources = False
    for suggestion in suggestions:
        classes = "w-full"
        if focus_property is not None and suggestion.property_name == focus_property:
            classes = "w-full bg-blue-1"
        with ui.card().classes(classes):
            with ui.row().classes("w-full items-center justify-between no-wrap"):
                with ui.column().classes("items-start no-wrap").style("min-width: 0"):
                    ui.label(property_heading(suggestion.property_name, suggestion.op, resolved)).classes("text-subtitle2")
                    caption = property_caption(suggestion.property_name, resolved)
                    if caption:
                        ui.label(caption).classes("text-caption text-grey-6")
                ui.badge(match_label(suggestion.match)).props(f"color={MATCH_COLORS[match_label(suggestion.match)]}")
            if suggestion.op in PROPERTY_OPS:
                _entity_value_label(f"Current: {full_value(suggestion.current_value)}", "text-body2 text-grey-7")
                _entity_value_label(f"Proposed: {full_value(suggestion.proposed_value)}", "text-body2")
            if suggestion.edited_value is not None:
                _entity_value_label(f"Edited: {suggestion.edited_value}", "text-body2 text-teal")
            if suggestion.merge_source_ids and not shown_merge_sources:
                shown_merge_sources = True
                ui.label(
                    f"Also deletes {len(suggestion.merge_source_ids)} merged source(s): "
                    f"{truncate(', '.join(suggestion.merge_source_ids), 200)}"
                ).classes("text-body2 text-negative")


def _navigate_entity(state: dict[str, Any], current: str, language: str | None, direction: int) -> None:
    """Move to the previous/next ROW within the current filter's row list.

    Navigation walks (sharedId, language) pairs, so it lands on the next row of
    the table the operator is looking at — for a translation task that means the
    sibling language of the same entity, not a different entity. The pairs come
    from the store's selection projection (the filter is a suboperation filter,
    so several parts of one group collapse to one pair).
    """
    pairs: list[tuple[str, str | None]] = []
    for _id, shared_id, row_language, _op in get_selection_rows(state["run_id"], _current_filter(state)):
        pair = (shared_id, row_language)
        if pair not in pairs:
            pairs.append(pair)
    if not pairs:
        return
    current_key = (current, language)
    index = pairs.index(current_key) if current_key in pairs else 0
    target_shared_id, target_language = pairs[(index + direction) % len(pairs)]
    _open_entity_window(state, target_shared_id, target_language)


__all__ = ["review_page"]
