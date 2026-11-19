"""Review-page module split out of the original ``review_page`` (behavior preserved)."""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger
from nicegui import background_tasks, context, ui

from uwazi_admin_agent.configuration import REVIEW_PAGE_SIZE
from uwazi_admin_agent.domain.drift import DriftStatus
from uwazi_admin_agent.domain.select_values import (
    rejected_after_toggle,
)
from uwazi_admin_agent.domain.suggestion import (
    DISPLAY_ONLY_OPS,
    FILE_OPS,
    PredictionAttemptStatus,
    Suggestion,
    SuggestionFilter,
    SuggestionStatus,
)
from uwazi_admin_agent.drivers.web.operation_gate import (
    active_text,
    busy_text,
    release,
    release_run,
    run_claim,
    run_claim_text,
    snapshot,
    try_acquire,
    try_claim_run,
)
from uwazi_admin_agent.drivers.web.operations_view import (
    EXPAND_COLUMN,
    STATUS_WITH_COUNT_COLUMN,
    entities_of_refs,
    ids_of_refs,
    languages_of_refs,
    operation_row,
    ops_of_refs,
    pairs_of_refs,
    rerun_blocked,
    sub_refs,
)
from uwazi_admin_agent.drivers.web.review_view import (
    ACTIONS_COLUMN,
    DRIFT_STATUS_COLORS,
    MODIFIED_COLUMN,
    OP_LABELS,
    OPERATION_SORT_OPTIONS,
    OPS_COLUMN,
    STATUS_COLORS,
    TEMPLATE_COLUMN,
    TITLE_COLUMN,
    bulk_discard_label,
    bulk_revert_label,
    cost_card_value,
    drift_banner_text,
    drift_banner_time,
    drift_text_classes,
    filter_from_controls,
    filter_options,
    pending_suboperation_count,
    run_button_label,
    run_confirmation,
    select_all_label,
    sort_direction_label,
    table_update_label,
)
from uwazi_admin_agent.drivers.web.run_service import (
    apply_outcomes,
    build_backup_store,
    check_drift,
    current_pass,
    discard_ids,
    get_drift_state,
    get_op_status_counts,
    get_operation_rows,
    get_run,
    get_selection_rows,
    get_suggestion_counts,
    get_table_update_time,
    preflight_selection,
    refresh_baseline_after_run,
    restore_ids,
    revert_entities_selection,
    update_table_from_uwazi,
)
from uwazi_admin_agent.drivers.web.session import (
    credentials_for_instance,
    page_instance_url,
    session_is_active,
)

from .dialogs import _reason_dialog, _simple_confirm

_RUNNABLE_STATUSES: frozenset[str] = frozenset(
    {
        SuggestionStatus.PENDING.value,
        SuggestionStatus.ACCEPTED.value,
        SuggestionStatus.EDITED.value,
        SuggestionStatus.REVERTED.value,
    }
)


_STATUS_LABELS: dict[str, str] = {status: status for status in STATUS_COLORS}


_RUN_STATUS_BADGE_COLORS: dict[str, str] = {
    "planned": "grey",
    "snapshotted": "orange",
    "executed": "green",
    "verified": "blue",
    "reverted": "indigo",
    "failed": "red",
    "generation_failed": "deep-orange",
}


def _accept_values_clicked(state: dict[str, Any], args: Any) -> None:
    """Accept every predicted value of ONE multiselect suboperation back."""
    from .entity_window import _set_rejections

    _row, sub = _sub_of(state, args)
    if sub is None or sub.get("is_info") or sub.get("is_display_only") or not isinstance(sub.get("id"), int):
        return
    background_tasks.create(
        _set_rejections(state, int(sub["id"]), [], context.client),
        name=f"accept values {sub['id']}",
    )


def _apply_checked(state: dict[str, Any], ids: list[int], checked: bool) -> None:
    """Add/remove SUBOPERATION ids from the checked set (in place)."""
    target: set[int] = state.setdefault("_checked", set())
    if checked:
        target.update(ids)
    else:
        target.difference_update(ids)


def _apply_drift_state(state: dict[str, Any], stored: Any, *, checking: bool = False) -> None:
    """Paint one verdict (or the checking glyph) onto the banner elements."""
    label = state.get("_drift_label")
    icon = state.get("_drift_icon")
    if label is None or label.is_deleted:
        return
    if checking:
        label.text = drift_banner_text(status="unknown", detail=None, checking=True)
        label.classes(replace=drift_text_classes("grey-7"))
        if icon is not None and not icon.is_deleted:
            icon.name = "sync"
            icon.classes("animate-spin text-body1")
            icon.color = "blue"
            icon.update()
        return
    if stored is None:
        label.text = drift_banner_text(status="unknown", detail=None)
        label.classes(replace=drift_text_classes("grey-7"))
        return
    label.text = drift_banner_text(
        status=stored.status.value,
        detail=stored.detail,
    ) + drift_banner_time(stored.checked_at)
    color = DRIFT_STATUS_COLORS.get(stored.status.value, "grey-7")
    icon_name = {
        "unknown": "help_outline",
        "valid": "check_circle",
        "drifted": "warning",
        "error": "error",
    }.get(stored.status.value, "help_outline")
    # The TEXT takes the verdict color: "Out of date" in red is the whole
    # point of the banner. ``replace=`` drops the resting grey so the verdict
    # color wins.
    label.classes(replace=drift_text_classes(color))
    if icon is not None and not icon.is_deleted:
        icon.name = icon_name
        # ``classes(...)`` ADDS by default: without removing the checking
        # stage's ``animate-spin`` here, the verdict icon would keep spinning
        # forever. A fresh class list also drops stale text-color entries so
        # the new verdict's color wins.
        icon.classes(replace="text-body1")
        icon.classes(f"text-{color}")
        icon.update()


def _backup_dates_for(rows: list[Any], run_id: str, url: str) -> dict[str, Any]:
    """``sharedId -> backup captured_at`` for the page's entities (local files only).

    One read attempt per entity (best-effort): an absent snapshot file means
    the run never wrote to that entity, so no tooltip date. Errors are logged,
    never raised — a missing date must not break the table render.
    """
    dates: dict[str, Any] = {}
    store = build_backup_store(url)
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
    from .entity_window import _revert_entity_clicked

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
            "name": "modified",
            "label": "Modified",
            "field": "modified",
            "align": "left",
            "sortable": False,
            "style": MODIFIED_COLUMN,
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
            <q-checkbox v-if="!props.row.is_display_only" v-model="props.selected" dense
                        :disable="!props.row.has_runnable"
                        :title="props.row.rerun_blocked ? props.row.rerun_blocked_reason
                                : (props.row.has_runnable ? '' : 'No runnable suboperation')" />
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
            <q-icon v-if="props.row.rerun_blocked" name="sync_disabled" size="16px" color="orange-9"
                    class="q-ml-xs" :title="props.row.rerun_blocked_reason" />
          </q-td>
          <q-td key="modified" :props="props" class="text-no-wrap">{{ props.row.modified }}</q-td>
          <q-td key="actions" :props="props" class="text-no-wrap">
            <template v-if="!props.row.is_display_only">
              <q-btn dense flat icon="play_arrow" color="primary"
                     :disable="!props.row.has_runnable"
                     :title="props.row.run_title"
                     @click.stop="$parent.$emit('run_op', props.row)" />
              <q-btn v-if="props.row.revertable" dense flat icon="undo" color="indigo"
                     :title="props.row.revert_tooltip || 'Revert this entity to its backup (all its languages)'"
                     @click.stop="$parent.$emit('revert_op', props.row)" />
              <q-btn v-if="props.row.run_status === 'discarded'" dense flat icon="restore" color="orange"
                     title="Undo the discard — these suboperations go back to the review queue"
                     @click.stop="$parent.$emit('restore_op', props.row)" />
              <q-btn v-else-if="props.row.run_status !== 'applied' && !props.row.rerun_blocked"
                     dense flat icon="block" color="negative"
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
                      <th class="text-left q-pa-xs">Value</th>
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
                      <tr v-else :key="sub.key">
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
                        <td class="q-pa-xs">
                          <template v-if="sub.select_type === 'multiselect'">
                            <div class="row items-center q-gutter-xs">
                              <q-chip v-for="v in sub.values" :key="v.label" dense
                                      :color="v.accepted ? 'green-2' : 'red-2'"
                                      :text-color="v.accepted ? 'green-10' : 'red-10'"
                                      :icon="v.accepted ? 'check' : 'close'">{{ v.label }}</q-chip>
                            </div>
                            <div class="text-caption text-grey-6">{{ sub.values.filter(v => v.accepted).length }} /
                              {{ sub.values.length }} values</div>
                          </template>
                          <div v-else class="text-body2">{{ sub.proposed }}</div>
                        </td>
                        <td class="q-pa-xs"><q-badge :color="sub.match_color">{{ sub.match }}</q-badge></td>
                        <td class="q-pa-xs"><q-badge :color="sub.status_color">{{ sub.status }}</q-badge></td>
                        <td class="q-pa-xs subs-actions">
                          <q-btn v-if="sub.selectable" dense flat icon="play_arrow" color="primary"
                                 title="Run this suboperation (only this property)"
                                 @click.stop="$parent.$emit('run_sub', props.row.row_key, sub.id)" />
                          <q-btn v-if="sub.has_rejected" dense flat icon="done_all" color="positive"
                                 title="Accept every predicted value again"
                                 @click.stop="$parent.$emit('accept_values', props.row.row_key, sub.id)" />
                          <q-btn v-if="sub.independent && sub.status === 'discarded'"
                                 dense flat icon="restore" color="orange"
                                 title="Undo the discard — this suboperation goes back to the review queue"
                                 @click.stop="$parent.$emit('restore_sub', props.row.row_key, sub.id)" />
                          <q-btn v-else-if="sub.selectable" dense flat icon="block" color="negative"
                                 title="Discard this suboperation — a Run will not execute it"
                                 @click.stop="$parent.$emit('discard_sub', props.row.row_key, sub.id)" />
                          <q-btn v-if="sub.revertable" dense flat icon="undo" color="indigo"
                                 title="Revert this one property to its backup value"
                                 @click.stop="$parent.$emit('revert_sub', props.row.row_key, sub.id)" />
                          <q-btn v-if="!sub.structural && !sub.is_display_only" dense flat icon="edit" color="teal"
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
    table.on("accept_values", lambda e: _accept_values_clicked(state, getattr(e, "args", None)))
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
        with ui.row().classes("items-center gap-1"):
            # First / previous / page readout / next / last. A run can have
            # dozens of pages, so the two ends are one click away instead of a
            # long run of chevrons.
            ui.button(icon="first_page", on_click=lambda: _page_to(state, 1)).props("flat dense").tooltip("First page")
            ui.button(icon="chevron_left", on_click=lambda: _page(state, -1)).props("flat dense")
            state["_page_number"] = ui.label("").classes("text-body2 q-mx-sm")
            ui.button(icon="chevron_right", on_click=lambda: _page(state, 1)).props("flat dense")
            ui.button(icon="last_page", on_click=lambda: _page_to(state, None)).props("flat dense").tooltip("Last page")
            # A typed page number: the fastest way to jump into the middle of a
            # 50-page table without stepping through every page.
            ui.label("Go to").classes("text-body2 text-grey-7 q-ml-sm")
            state["_page_input"] = (
                ui.number(
                    value=1,
                    min=1,
                    max=1,
                    precision=0,
                    on_change=lambda e: _page_from_input(state, e.value),
                )
                .props("dense outlined")
                .classes("w-24")
                .tooltip("Type a page number and press Enter")
            )
    _reload_table(state)


async def _check_drift_now(state: dict[str, Any], notify: bool = True, client: Any = None) -> None:
    """Run the drift check in the background thread and repaint the banner.

    ``client`` is captured by the caller INSIDE the page's slot context (the
    same pattern ``_run_rows`` uses): a background task starts with an empty
    slot stack, so every UI call it makes — the banner updates and the
    toasts — must re-enter the client explicitly via ``with client:``.
    """
    credentials = credentials_for_instance(state["instance_url"])
    if credentials is None:
        _apply_drift_state(
            state,
            None,
        )
        _set_drift_error(state, "Drift check failed — not logged in to Uwazi")
        return
    _apply_drift_state(state, None, checking=True)
    try:
        # ``check_drift`` is SYNCHRONOUS (it blocks on Uwazi HTTP calls), so it
        # must run in a worker thread, like every other service call (the
        # apply_outcomes/revert_run pattern) — awaiting it directly raised
        # "object DriftStateRow can't be used in 'await' expression".
        updated = await asyncio.to_thread(
            check_drift,
            state["instance_url"],
            state["run_id"],
            credentials.user,
            credentials.password,
            "auto" if not notify else "manual",
        )
    except Exception as exc:  # noqa: BLE001 — the banner reports; the page never crashes
        logger.exception("drift check failed")
        _set_drift_error(state, f"Drift check failed — {exc}")
        return
    _apply_drift_state(state, updated)
    if not notify or client is None:
        return
    try:
        with client:
            if updated.status.value == "drifted":
                # The toast matches the banner's red "Out of date" verdict.
                ui.notify(updated.detail or "Drift detected in Uwazi", type="negative")
            elif updated.status.value == "valid":
                ui.notify("No drift detected — table is current", type="positive")
    except RuntimeError as exc:  # noqa: BLE001 — a disconnected client cannot show a toast
        logger.debug("could not notify drift verdict (client gone): {}", exc)


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


def _claim_gate(state: dict[str, Any], kind: str, client: Any) -> bool:
    """Claim this run, then the instance's single Uwazi-write slot.

    A refusal is shown HERE, in the review UI, and names the real conflict:
    the same run already has an operation, or another write is in progress on
    this instance. A write on a different instance is never a conflict.
    """
    instance = state["instance_url"]
    run_id = state["run_id"]
    if not try_claim_run(instance, run_id, kind):
        with client:
            ui.notify(run_claim_text(run_claim(instance, run_id)), type="warning", multi_line=True)
        return False
    if not try_acquire(instance, kind, run_id):
        with client:
            ui.notify(busy_text(snapshot(instance)), type="warning", multi_line=True)
        release_run(instance, run_id, kind)
        return False
    return True


def _claim_run_only(state: dict[str, Any], kind: str, client: Any) -> bool:
    """Claim only this RUN (no Uwazi write gate): reads like a table sync.

    A local/no-write operation may proceed while this instance runs a write on
    another run, but it still cannot touch a run that is already busy.
    """
    instance = state["instance_url"]
    run_id = state["run_id"]
    if try_claim_run(instance, run_id, kind):
        return True
    with client:
        ui.notify(run_claim_text(run_claim(instance, run_id)), type="warning", multi_line=True)
    return False


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


def _current_filter(state: dict[str, Any], *, accepted_only: bool = False) -> SuggestionFilter:
    return filter_from_controls(
        status=state["status"],
        op=state["op"],
        property_name=state["property"],
        language=state["language"],
        text=state["text"],
        offset=state["offset"],
        limit=REVIEW_PAGE_SIZE,
        accepted_only=accepted_only,
        sort_by=state["sort_by"],
        sort_desc=state["sort_desc"],
    )


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
        f"Mark {len(ids)} checked {noun} as discarded. They will NOT be applied by a Run, "
        "and the reason is stored with them.",
        _submit,
        title=f"Discard {len(ids)} {noun}",
        confirm_label="Discard",
        reason_label="Reason (optional — why the end user does not want these applied)",
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


def _discard_op_clicked(state: dict[str, Any], row_key: str) -> None:
    """Mark ONE operation as discarded (its visible suboperations).

    The filter is a SUBOPERATION filter, so "the operation" here means the
    parts the table is showing: discarding an operation under a property filter
    refuses those property writes, not the ones the filter hid.
    """
    row = _row_of_key(state, row_key)
    if row is None or row.get("is_display_only") or row.get("is_info"):
        return
    if row.get("rerun_blocked"):
        ui.notify(f"This operation cannot be run again — {row.get('rerun_blocked_reason')}", type="warning")
        return
    ids = ids_of_refs(_row_refs(row))
    if not ids:
        return

    def _submit(reason: str | None) -> None:
        client = context.client
        background_tasks.create(_discard_ids(state, ids, reason, client), name=f"discard {row_key}")

    _reason_dialog(
        "Mark this operation as discarded? A Run will not apply it. The reason is stored with it.",
        _submit,
        title="Discard operation",
        confirm_label="Discard",
        reason_label="Reason (optional — why the end user does not want this applied)",
    )


def _discard_sub_clicked(state: dict[str, Any], args: Any) -> None:
    """Discard ONE suboperation (its id is the whole selection)."""
    _row, sub = _sub_of(state, args)
    if sub is None or sub.get("is_info") or sub.get("is_display_only") or not isinstance(sub.get("id"), int):
        return
    sub_id = int(sub["id"])
    client = context.client
    background_tasks.create(
        _discard_ids(state, [sub_id], "discarded from the suboperations table", client),
        name=f"discard sub {sub_id}",
    )


def _drift_banner(state: dict[str, Any]) -> None:
    """The drift line: what the last drift check concluded.

    It sits BELOW the action bar, just above the table — the state of the data
    the table shows. Renders the STORED verdict (or "not checked yet"); the
    open-page auto-check and the manual button update it. One row: an icon +
    the text + (right) the check-drift and sync buttons.
    """
    with ui.row().classes("w-full items-center gap-2 q-mb-sm"):
        state["_drift_icon"] = ui.icon("sync", color="grey-6").classes("text-body1")
        state["_drift_label"] = ui.label(drift_banner_text(status="unknown", detail=None)).classes(
            drift_text_classes("grey-7")
        )
        # The verdict color is painted on the TEXT as well as the icon (see
        # ``_apply_drift_state``): "Out of date" must be impossible to miss,
        # and grey body text on a light table reads as ordinary prose.
        state["_table_update_label"] = ui.label(
            table_update_label(get_table_update_time(page_instance_url(), state["run_id"]))
        ).classes("text-caption text-grey-6")

    # The verdict's text is stored per run: apply it immediately, then let the
    # open-page auto-check refresh it (a fresh page should not show a stale
    # 'valid' while Uwazi moved — the check replaces the text in place).
    stored = get_drift_state(page_instance_url(), state["run_id"])
    _apply_drift_state(state, stored, checking=stored is None)
    if stored is None or stored.status is DriftStatus.UNKNOWN or stored.checked_by == "sync":
        background_tasks.create(_check_drift_now(state, notify=False, client=context.client))
    elif stored.status is not DriftStatus.VALID:
        # A previous page visit already found drift; re-check in the background
        # so the operator sees CURRENT drift rather than yesterday's.
        background_tasks.create(_check_drift_now(state, notify=False, client=context.client))


def _edit_sub_clicked(state: dict[str, Any], args: Any) -> None:
    """Open the entity window AT the suboperation's language, focused on its property."""
    from .entity_window import _open_entity_window

    _row, sub = _sub_of(state, args)
    if sub is None or sub.get("is_info") or sub.get("is_display_only"):
        return
    _open_entity_window(
        state,
        str(sub.get("shared_id") or ""),
        sub.get("lang"),
        focus_property=sub.get("property_name"),
    )


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


def _modal_header(dialog: Any, title: str) -> None:
    """Modal title row with a top-right Close button (matches app.py's helper)."""
    with ui.row().classes("w-full items-center justify-between no-wrap shrink-0"):
        ui.label(title).classes("text-h6")
        ui.button("Close", icon="close", on_click=dialog.close).props("flat")


def _on_table_select(state: dict[str, Any], event: Any) -> None:
    """The table's selection callback: only the counters need refreshing.

    The cascade to the suboperations lives in :func:`_selection_changed`, which
    listens to the raw event carrying WHICH rows were added or removed.
    """
    _refresh_selection_counts(state)


def _open_entity_row(state: dict[str, Any], row: dict[str, Any]) -> None:
    """Open the entity window for one operation row (its first written language)."""
    from .entity_window import _open_entity_window

    if row.get("is_info"):
        return
    shared_id = str(row.get("shared_id") or "")
    languages = [lang for lang in (row.get("languages") or []) if lang]
    _open_entity_window(state, shared_id, languages[0] if languages else None)


def _page(state: dict[str, Any], direction: int) -> None:
    """Step one page forward/backward, staying inside the result set."""
    current = state["offset"] // REVIEW_PAGE_SIZE + 1
    _page_to(state, current + direction)


def _page_count(state: dict[str, Any]) -> int:
    """How many pages the current result set has (at least one)."""
    return max((int(state.get("total") or 0) + REVIEW_PAGE_SIZE - 1) // REVIEW_PAGE_SIZE, 1)


def _page_from_input(state: dict[str, Any], value: Any) -> None:
    """The typed page number's handler (ignores the programmatic rewrite)."""
    if state.get("_suppress_page_events"):
        return
    if value is None:
        _refresh_page_controls(state)
        return
    try:
        page = int(float(value))
    except (TypeError, ValueError):
        _refresh_page_controls(state)
        return
    _page_to(state, page)


def _page_to(state: dict[str, Any], page: int | None) -> None:
    """Jump to a page (``None`` = the last one), clamped to the result set."""
    pages = _page_count(state)
    target = pages if page is None else int(page)
    target = max(1, min(target, pages))
    new_offset = (target - 1) * REVIEW_PAGE_SIZE
    if new_offset == state["offset"]:
        _refresh_page_controls(state)
        return
    state["offset"] = new_offset
    _reload_table(state)


def _poll_gate_toast(state: dict[str, Any]) -> None:
    """Keep ONE live toast naming the in-flight Uwazi operation.

    The gate's snapshot is polled at 0.8 s and the SAME notification element is
    updated in place with the fresh fraction and its filling bar. It is
    deliberately not a Quasar *group*: grouped notifications accumulate a badge
    counting every emission, so a run of a few thousand rows ended up showing a
    "44" bubble — noise, not progress. One persistent element means exactly one
    toast for the whole pass, dismissed as soon as the gate goes idle.
    """
    active = snapshot(state.get("instance_url", ""))
    note = state.get("_gate_notification")
    if active is None:
        state["_gate_signature"] = None
        if note is not None:
            state["_gate_notification"] = None
            if not note.is_deleted:
                try:
                    note.dismiss()
                except Exception:  # noqa: BLE001 — a gone client cannot dismiss a toast
                    logger.debug("could not dismiss the gate toast")
        return  # nothing running; the live toast is gone
    signature = (active.kind, active.run_id, active.done, active.total, active.note)
    if signature == state.get("_gate_signature") and note is not None and not note.is_deleted:
        return
    state["_gate_signature"] = signature
    text = active_text(active)
    progress = (active.done / active.total) if active.total > 0 else True
    if note is None or note.is_deleted:
        note = ui.notification(
            text,
            type="ongoing",
            spinner=True,
            timeout=0,
            multi_line=True,
            progress=progress,
        )
        state["_gate_notification"] = note
        return
    note.message = text
    # ``Notification`` exposes message/type/spinner setters but no progress
    # setter; its options dict IS the Quasar payload the component updates with.
    note._props["options"]["progress"] = progress
    note.update()


def _poll_progress(state: dict[str, Any]) -> None:
    """Refresh the header while a pass is RUNNING (progress + spend).

    A logged-out session (or one that switched instance in another tab) stops
    reading: the timer keeps firing until the page unloads, and it must not
    serve the previous instance's table to a page nobody is authenticated for.
    """
    if not session_is_active(state["instance_url"]):
        return
    pass_row = current_pass(state["instance_url"], state["run_id"])
    if pass_row is None or pass_row.status != PredictionAttemptStatus.RUNNING:
        return
    _render_summary(state)
    _reload_table(state)


def _refresh_all(state: dict[str, Any]) -> None:
    """Re-render the run badge, the summary, the selection buttons, and the page."""
    if not session_is_active(state["instance_url"]):
        return
    try:
        _refresh_run_badge(state)
        _render_summary(state)
        _refresh_filter_counts(state)
        if state["offset"] >= state["total"] and state["offset"] > 0:
            state["offset"] = (_page_count(state) - 1) * REVIEW_PAGE_SIZE
        _reload_table(state)
    except Exception:  # noqa: BLE001 — a refresh failure must not break the page
        logger.exception("review refresh failed")


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


def _refresh_filter_counts(state: dict[str, Any]) -> None:
    """Relabel the Op/Status options with fresh whole-run counts (best-effort).

    The counts move when rows change status (accept, discard, run, revert), so
    the controls are rebuilt after every refresh. ``set_options`` keeps the
    current selection, and :func:`filter_options` keeps a selected value listed
    even at ``(0)``, so a filter is never silently cleared.
    """
    try:
        counts = get_op_status_counts(page_instance_url(), state["run_id"])
    except Exception:  # noqa: BLE001 — labels must not break a table refresh
        logger.exception("could not refresh the filter counts")
        return
    for key, labels in (("op", OP_LABELS), ("status", _STATUS_LABELS)):
        widget = state.get(f"_{key}_select")
        if widget is None or widget.is_deleted:
            continue
        widget.set_options(filter_options(labels, counts.get(key, {}), state[key]))


def _refresh_page_controls(state: dict[str, Any]) -> None:
    """Write the current page into the readout and the typed page input."""
    pages = _page_count(state)
    page = state["offset"] // REVIEW_PAGE_SIZE + 1
    label = state.get("_page_number")
    if label is not None and not label.is_deleted:
        label.text = f"page {page} / {pages}"
    page_input = state.get("_page_input")
    if page_input is None or page_input.is_deleted:
        return
    state["_suppress_page_events"] = True
    try:
        page_input.min = 1
        page_input.max = pages
        page_input.value = page
    finally:
        state["_suppress_page_events"] = False


def _refresh_run_badge(state: dict[str, Any]) -> None:
    """Keep the header's run-status badge honest after a partial/full revert.

    A per-entity or per-property revert can flip the run to REVERTED; the badge
    used to be rendered once at page load and kept saying "executed".
    """
    badge = state.get("_status_badge")
    if badge is None or badge.is_deleted:
        return
    try:
        detail = get_run(page_instance_url(), state["run_id"])
    except Exception:  # noqa: BLE001 — a badge refresh must not break the page
        return
    badge.text = detail.status.value
    badge.props(f"color={_RUN_STATUS_BADGE_COLORS.get(detail.status.value, 'grey-7')}")


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


def _refresh_sort_controls(state: dict[str, Any]) -> None:
    """Make the direction button say which order the table is in."""
    button = state.get("_sort_direction_button")
    if button is None or button.is_deleted:
        return
    sort_by = state.get("sort_by") or "recorded"
    descending = bool(state.get("sort_desc"))
    button.set_icon("arrow_downward" if descending else "arrow_upward")
    button.text = sort_direction_label(sort_by, descending)


def _reload_table(state: dict[str, Any]) -> None:
    """Load one page of OPERATIONS with their suboperations and update the table.

    The filter is a SUBOPERATION filter, so an operation whose every part the
    filter removed never reaches the page — "no suboperations left means no
    row", enforced by the store. The rows carry each entity's public Uwazi URL
    (read once here, so the table costs no extra request for it).
    """
    from .entity_window import _labels_for_operation, _types_for_operation

    if state.get("loading"):
        return
    state["loading"] = True
    try:
        rows, total = get_operation_rows(page_instance_url(), state["run_id"], _current_filter(state))
        state["total"] = total
        base_url = page_instance_url()
        # Per-row Revert tooltip: each page reads its entities' backup dates (a
        # local file read per entity — no HTTP). Entities without a backup
        # (never written) carry None and their Revert tooltip falls back to the
        # generic text.
        tooltips = _backup_dates_for(rows, state["run_id"], page_instance_url())
        state["_backup_dates"] = tooltips
        labels_by_template = state.get("_template_labels") or {}
        types_by_template = state.get("_template_types") or {}
        payloads = [
            operation_row(
                item,
                checked_ids=state.get("_checked") or (),
                uwazi_url=base_url,
                labels=_labels_for_operation(item, labels_by_template, state.get("_labels") or {}),
                types=_types_for_operation(item, types_by_template, state.get("_types") or {}),
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
                    if isinstance(sub.get("id"), int) and sub.get("runnable")
                }
                state["_checked"] = {sub_id for sub_id in (state.get("_checked") or ()) if sub_id in visible}
            _sync_selection(state)
        label = state.get("_page_label")
        if label is not None and not label.is_deleted:
            first = state["offset"] + 1 if total else 0
            last = min(state["offset"] + REVIEW_PAGE_SIZE, total)
            label.text = f"Showing {first:,}–{last:,} of {total:,} operations"
        _refresh_page_controls(state)
        _refresh_sort_controls(state)
        _refresh_expand_button(state)
    finally:
        state["loading"] = False


def _render_summary(state: dict[str, Any]) -> None:
    holder = state.get("_summary")
    if holder is None or holder.is_deleted:
        return
    counts = get_suggestion_counts(page_instance_url(), state["run_id"])
    pass_row = current_pass(page_instance_url(), state["run_id"])
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
            caption=(
                f"{counts.get('operations_total', 0):,} entities to run"
                f" · {counts.get('result_entities', 0):,} result entities"
            ),
        )
        _card("Pending", f"{pending_suboperation_count(counts):,}", "grey")
        _card("Matched", f"{counts.get('display_matched', 0):,}", "green")
        _card("Not processed", f"{counts.get('display_skipped', 0):,}", "orange")
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


def _reset_global_selection(state: dict[str, Any]) -> None:
    """Drop a whole-result selection when the filter that defined it changes."""
    if state.get("_select_all"):
        _clear_selection(state, clear_page=True)


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


def _restore_op_clicked(state: dict[str, Any], row_key: str) -> None:
    """Undo the discard of ONE operation (its visible rows go back to pending)."""
    row = _row_of_key(state, row_key)
    if row is None or row.get("is_display_only") or row.get("is_info"):
        return
    ids = ids_of_refs(_row_refs(row))
    client = context.client
    background_tasks.create(_restore_ids(state, ids, client), name=f"restore {row_key}")


def _restore_sub_clicked(state: dict[str, Any], args: Any) -> None:
    """Undo ONE suboperation's discard."""
    _row, sub = _sub_of(state, args)
    if sub is None or sub.get("is_info") or sub.get("is_display_only") or not isinstance(sub.get("id"), int):
        return
    client = context.client
    background_tasks.create(_restore_ids(state, [int(sub["id"])], client), name=f"restore sub {sub['id']}")


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
    from .entity_window import _refresh_entity_window

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
            outcome = await revert_entities_selection(
                state["instance_url"],
                state["run_id"],
                [(sid, None) for sid in entities],
                credentials.user,
                credentials.password,
                progress=state["_progress"],
            )
            with client:
                ui.notify(
                    f"Revert done — {outcome.summary()}",
                    type="positive" if not outcome.failures else "warning",
                    multi_line=True,
                )
        except Exception as exc:  # noqa: BLE001 — the toast reports; the page never crashes
            logger.exception("bulk revert failed")
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


def _revert_sub_clicked(state: dict[str, Any], args: Any) -> None:
    """Revert ONE property's write (the suboperation's own undo)."""
    from .entity_window import _revert_suboperation_clicked

    _row, sub = _sub_of(state, args)
    if (
        sub is None
        or sub.get("is_info")
        or sub.get("is_display_only")
        or not sub.get("revertable")
        or not isinstance(sub.get("id"), int)
    ):
        return
    _revert_suboperation_clicked(
        state,
        str(sub.get("shared_id") or ""),
        sub.get("lang"),
        str(sub.get("property_name") or ""),
        int(sub["id"]),
        str(sub.get("label") or "this property"),
    )


def _row_key_of(args: Any) -> str:
    """The row_key out of a table event's args (a dict for row emits, a list for sub emits)."""
    if isinstance(args, dict):
        return str(args.get("row_key") or "")
    if isinstance(args, list) and args:
        return str(args[0])
    return ""


def _row_of(event: Any) -> dict[str, Any]:
    """The clicked row dict out of a table event (NiceGUI sends it as ``args``)."""
    args = event.args
    return args if isinstance(args, dict) else {}


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


def _run_from_state(state: dict[str, Any]) -> None:
    """Open the Run confirmation for the selection the button's count describes."""
    refs, ops = _selection_snapshot(state)
    _run_refs(state, refs, ops)


def _run_op_clicked(state: dict[str, Any], row_key: str) -> None:
    """The operation's Run button: run the parts the operation covers.

    The parts the operator ticked (plus the locked parts, which run with the
    operation) are written; when NOTHING checkable is ticked, the button means
    the whole operation — the only reading that makes a row-level Run useful.
    """
    row = _row_of_key(state, row_key)
    if row is None:
        return
    if row.get("is_display_only") or row.get("is_info"):
        ui.notify("This row reports a result and is display-only", type="info")
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
        blocked_reason = next(
            (str(sub.get("rerun_blocked_reason")) for sub in subs if sub.get("rerun_blocked_reason")),
            "",
        )
        if blocked_reason:
            ui.notify(f"This operation cannot be run again — {blocked_reason}", type="warning", multi_line=True)
            return
        ui.notify("Nothing left to run in this operation", type="warning")
        return
    _set_children_checked(state, row, True)
    _sync_selection(state)
    _run_refs(state, sub_refs(covered))


def _run_refs(
    state: dict[str, Any],
    refs: list[tuple[int, str, str | None, str]],
    ops: list[str] | None = None,
    confirm: bool = True,
) -> None:
    """Execute the given SUBOPERATIONS with the Run heuristic (revertable).

    The Run action IS the acceptance: ``ApplySuggestionsUseCase.apply`` accepts
    the selected rows' PENDING state as it runs them. The selection carries both
    the entity-language PAIRS (how the apply path addresses a row) and the
    suggestion IDS (how it narrows below the pair), so running one property of a
    translation row writes exactly that property.

    ``confirm=False`` writes immediately, with no confirmation dialog. The
    entity window's Run buttons use it: that window is a per-entity working
    surface where the operator runs one row or one property at a time, and a
    modal on every press made the loop slower than the write it guards. The
    table's Run (a whole selection) keeps its confirmation.
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
            _run_rows(state, pairs, ids, client, resolved_ops),
            name=f"run {state['run_id']}",
        )

    if not confirm:
        _confirm()
        return

    title, message = run_confirmation(len(ids), resolved_ops, languages)
    _simple_confirm(title, message, _confirm)


async def _run_rows(
    state: dict[str, Any],
    pairs: list[tuple[str, str | None]],
    ids: list[int],
    client: Any,
    ops: list[str] | None = None,
) -> None:
    from .entity_window import _refresh_entity_window

    credentials = credentials_for_instance(state["instance_url"])
    if credentials is None:
        with client:
            ui.notify("Not logged in to Uwazi", type="negative")
        return
    # One Uwazi-write operation at a time per instance (the gate): a concurrent
    # Run/Revert on THIS instance is refused with its live count; another
    # instance's pass is never a conflict.
    if not _claim_gate(state, "run", client):
        return
    # The preflight (a fresh read of the selected rows) runs BEFORE any
    # progress unit exists: name the phase on the live toast so the operator
    # sees the Run thinking instead of a bare indeterminate spinner (a
    # highlight of what the extract-date task's slow bulk refusal once wore).
    state["_progress"](0, 0, "verifying the selection against Uwazi")
    try:
        try:
            preflight = await asyncio.to_thread(
                preflight_selection,
                state["instance_url"],
                state["run_id"],
                ids,
                credentials.user,
                credentials.password,
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
            await apply_outcomes(
                state["instance_url"],
                state["run_id"],
                credentials.user,
                credentials.password,
                pairs,
                ids,
                progress=state["_progress"],
            )
            # The writes have landed: say so NOW, before any bookkeeping, so the
            # operator can read the outcome and move on to the next row while the
            # run finishes its tail.
            with client:
                if FILE_OPS.intersection(ops or []):
                    # A file copy appends bytes: no entity snapshot can remove
                    # them, so do not point the operator at a Revert that
                    # cannot undo this part.
                    ui.notify(
                        "Applied — the files were copied onto the target and stay there",
                        type="positive",
                        multi_line=True,
                    )
                else:
                    ui.notify(
                        f"Applied — 'Revert' on run {state['run_id']!r} (or Revert task) undoes it",
                        type="positive",
                        multi_line=True,
                    )
            # The writes bumped Uwazi editDates: re-sync the baseline so the run's
            # own writes are the new reference, not drift. That is a PAGED
            # ``/api/v2/search`` per language, so it runs in a THREAD: on the event
            # loop it froze the page (no clicks, no row navigation) for the whole
            # scan — the same reason the table refresh threads it. Bookkeeping
            # never turns a landed Run into a failure.
            try:
                await asyncio.to_thread(
                    refresh_baseline_after_run,
                    state["instance_url"],
                    state["run_id"],
                    credentials.user,
                    credentials.password,
                )
            except Exception as exc:  # noqa: BLE001 — the writes landed; the baseline is best-effort
                logger.warning("post-run baseline sync failed for {}: {}", state["run_id"], exc)
        except Exception as exc:  # noqa: BLE001
            logger.exception("run (heuristic apply) failed")
            with client:
                ui.notify(
                    f"Run failed: {exc}",
                    type="negative",
                    multi_line=True,
                )
    finally:
        release(state["instance_url"], "run", state["run_id"])
        release_run(state["instance_url"], state["run_id"], "run")
        with client:
            _clear_checked(state)
            _refresh_all(state)
            _refresh_entity_window(state)


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
    if sub is None or sub.get("is_info") or sub.get("is_display_only"):
        return
    if not _runnable(sub):
        blocked_reason = str(sub.get("rerun_blocked_reason") or "")
        if blocked_reason:
            ui.notify(f"This row cannot be run again — {blocked_reason}", type="warning", multi_line=True)
            return
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


def _runnable(sub: dict[str, Any]) -> bool:
    """Whether a suboperation payload is still waiting to be written (pure).

    A reverted delete/merge part is NOT runnable: its revert re-created an
    entity with a fresh sharedId, so the recorded row still points at the old
    one. The payload carries the flag; the status alone would wrongly say the
    write can be repeated.
    """
    return str(sub.get("status") or "") in _RUNNABLE_STATUSES and not sub.get("rerun_blocked")


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


def _selection_snapshot(
    state: dict[str, Any],
) -> tuple[list[tuple[int, str, str | None, str]], list[str]]:
    """What Run would act on right now: the checked suboperations and their ops."""
    refs = _checked_refs(state)
    return refs, ops_of_refs(refs)


def _set_children_checked(state: dict[str, Any], row: dict[str, Any], checked: bool) -> None:
    """Tick/untick every part of one operation.

    A tick is a SELECTION, not an acceptance: the parts' statuses are left
    alone. Locked parts are included because the operation's Run covers them —
    they simply have no checkbox of their own to show it. Info rows are
    display-only and carry no selection at all.
    """
    if row.get("is_display_only") or row.get("is_info"):
        return
    # Only parts a Run can still write enter the selection. An applied part
    # (already in Uwazi), a discarded one (restore first) or an obsolete one
    # must never inflate Run (N) / Discard (N); locked parts of a RUNNABLE
    # operation still ride their operation's tick.
    ids = [
        int(sub["id"])
        for sub in (row.get("subs") or [])
        if isinstance(sub, dict) and sub.get("runnable") and isinstance(sub.get("id"), int)
    ]
    _apply_checked(state, ids, checked)


def _set_drift_error(state: dict[str, Any], text: str) -> None:
    """Write a failed-check message onto the banner, in red (pure-ish).

    The check failed, so the banner must not keep the resting grey: a red
    line says "the state below may be stale".
    """
    label = state.get("_drift_label")
    if label is None or label.is_deleted:
        return
    label.text = text
    label.classes(replace=drift_text_classes(DRIFT_STATUS_COLORS["error"]))


def _set_sort(state: dict[str, Any], sort_by: str) -> None:
    """Order the table by one column (server-side, across every page)."""
    if sort_by not in OPERATION_SORT_OPTIONS:
        sort_by = "recorded"
    previous = state.get("sort_by") or "recorded"
    state["sort_by"] = sort_by
    # Modified reads naturally newest-first; the other columns start A→Z. The
    # direction button still flips either.
    if sort_by == "modified" and previous != "modified":
        state["sort_desc"] = True
    elif sort_by != "modified" and previous == "modified":
        state["sort_desc"] = False
    state["offset"] = 0
    _reload_table(state)


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


def _suggestion_is_runnable(suggestion: Suggestion) -> bool:
    """Whether a Run can still write this row (pure).

    A reverted delete/merge row cannot be executed again: its revert re-created
    an entity with a new sharedId, and the row still names the old one.
    """
    return (
        suggestion.id is not None
        and suggestion.op not in DISPLAY_ONLY_OPS
        and suggestion.status.value in _RUNNABLE_STATUSES
        and not rerun_blocked(suggestion.op, suggestion.status.value)
    )


def _summary_row(state: dict[str, Any]) -> None:
    holder = ui.row().classes("w-full items-center gap-3 q-mb-sm")
    state["_summary"] = holder
    _render_summary(state)


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
        if row.get("is_display_only") or row.get("is_info"):
            # Display-only rows are never part of a selection.
            continue
        subs = row.get("subs") or []
        for sub in subs:
            if isinstance(sub, dict):
                sub["checked"] = bool(sub.get("selectable")) and sub.get("id") in checked
        runnable_subs = [sub for sub in subs if isinstance(sub, dict) and sub.get("runnable")]
        if runnable_subs and all(sub.get("id") in checked for sub in runnable_subs):
            selected.append(row)
    table.selected = selected
    table.update()
    _refresh_selection_counts(state)


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


def _toggle_select_all(state: dict[str, Any]) -> None:
    """Select every SUBOPERATION matching the filter across pages, or clear it."""
    if state.get("_select_all"):
        _clear_selection(state, clear_page=True)
        return
    if int(state.get("total") or 0) <= 0:
        return
    # The projection is a row-id list, not a page: selection_rows ignores
    # limit/offset, so this covers every operation the filter matches. Only
    # runnable rows are selected: applied/discarded/obsolete parts cannot be
    # written by a Run and must not inflate the action counts.
    refs = get_selection_rows(
        page_instance_url(),
        state["run_id"],
        _current_filter(state),
        runnable_only=True,
    )
    state["_global_refs"] = {ref[0]: ref for ref in refs}
    state["_checked"] = {ref[0] for ref in refs}
    state["_select_all"] = True
    _sync_selection(state)


def _toggle_sort_direction(state: dict[str, Any]) -> None:
    """Flip the current sort between ascending and descending."""
    state["sort_desc"] = not bool(state.get("sort_desc"))
    state["offset"] = 0
    _reload_table(state)


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


def _toggle_value(state: dict[str, Any], suggestion: Suggestion, label: str, accepted: bool) -> None:
    """Apply ONE checkbox's decision for a multiselect row (thin wrapper).

    The ``rejected`` list is built from the suggestion object the checkbox was
    built with: the window re-renders after every write, so the widget's row and
    the stored row agree.
    """
    from .entity_window import _set_rejections

    if suggestion.id is None:
        return
    rejected = rejected_after_toggle(suggestion.rejected_values, label, accepted)
    background_tasks.create(
        _set_rejections(state, suggestion.id, rejected, context.client),
        name=f"toggle value {suggestion.id}",
    )


async def _update_table_now(state: dict[str, Any], client: Any) -> None:
    """Sync the table in place with Uwazi, then repaint the label, drift badge and grid."""
    credentials = credentials_for_instance(state["instance_url"])
    if credentials is None:
        with client:
            ui.notify("Not logged in to Uwazi", type="negative")
        return
    item = state.get("_update_table_item")
    label = state.get("_table_update_label")
    # Syncing only READS Uwazi, so it takes no write gate: another run's write
    # pass must neither block it nor be blocked by it. It still takes this
    # run's claim, so the same run cannot be synced and Run at once.
    if not _claim_run_only(state, "refresh", client):
        return
    try:
        with client:
            if item is not None and not item.is_deleted:
                item.disable()
            if label is not None and not label.is_deleted:
                label.text = "Syncing with Uwazi…"
        result = await update_table_from_uwazi(
            state["instance_url"], state["run_id"], credentials.user, credentials.password
        )
        updated = get_drift_state(state["instance_url"], state["run_id"])
        with client:
            _apply_drift_state(state, updated)
            if label is not None and not label.is_deleted:
                label.text = table_update_label(get_table_update_time(state["instance_url"], state["run_id"]))
            ui.notify(result.message(), type="positive", multi_line=True)
            _refresh_all(state)
    except Exception as exc:  # noqa: BLE001 — the page reports, never crashes
        logger.exception("sync with Uwazi failed")
        with client:
            ui.notify(
                f"Sync with Uwazi failed: {exc}",
                type="negative",
                multi_line=True,
            )
    finally:
        release_run(state["instance_url"], state["run_id"], "refresh")
        if item is not None and not item.is_deleted:
            with client:
                item.enable()
