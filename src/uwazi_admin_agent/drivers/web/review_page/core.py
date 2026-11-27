"""Review-page module split out of the original ``review_page`` (behavior preserved)."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from loguru import logger
from nicegui import background_tasks, context, ui

from uwazi_admin_agent.configuration import REVIEW_PAGE_SIZE
from uwazi_admin_agent.domain.drift import DriftStatus
from uwazi_admin_agent.domain.relationship_values import RELATIONSHIP_TYPE
from uwazi_admin_agent.domain.suggestion import (
    CREATE_OPS,
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
    entities_of_refs,
    ids_of_refs,
    languages_of_refs,
    operation_row,
    ops_of_refs,
    pairs_of_refs,
    recommendation_text,
    rerun_blocked,
    sub_refs,
)
from uwazi_admin_agent.drivers.web.review_view import (
    OP_LABELS,
    STATUS_COLORS,
    drift_banner_text,
    drift_banner_time,
    expanded_keys_for,
    expansion_mode,
    filter_from_controls,
    filter_options,
    language_filter_options,
    performance_visible,
    property_filter_options,
    review_sort_pagination,
    run_button_label,
    run_confirmation,
    select_all_label,
    sort_from_header_click,
    table_update_label,
)
from uwazi_admin_agent.drivers.web.run_service import (
    apply_outcomes,
    build_backup_store,
    check_drift,
    current_pass,
    discard_ids,
    get_drift_state,
    get_filter_counts,
    get_operation_rows,
    get_run,
    get_selection_rows,
    get_table_update_time,
    preflight_selection,
    refresh_baseline_after_run,
    restore_ids,
    revert_entities_selection,
    update_table_from_uwazi,
    validation_summary,
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
        SuggestionStatus.REVERTED.value,
    }
)


_STATUS_LABELS: dict[str, str] = {status: status for status in STATUS_COLORS}
# The Status filter also names the validation rows' comparison verdicts: their
# lifecycle status is not the operator's question, the frozen-label match is.
_STATUS_LABELS.update({"match": "match", "mismatch": "mismatch"})


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
    from .entity_window import _set_selection_by_id

    _row, sub = _sub_of(state, args)
    if sub is None or sub.get("is_info") or sub.get("is_display_only") or not isinstance(sub.get("id"), int):
        return
    background_tasks.create(
        _set_selection_by_id(state, int(sub["id"]), None, context.client),
        name=f"accept values {sub['id']}",
    )


def _apply_checked(state: dict[str, Any], ids: list[int], checked: bool) -> None:
    """Add/remove SUBOPERATION ids from the checked set (in place)."""
    target: set[int] = state.setdefault("_checked", set())
    if checked:
        target.update(ids)
    else:
        target.difference_update(ids)


_DRIFT_LABELS: dict[str, str] = {
    "valid": "Up to date",
    "drifted": "Out of date",
    "error": "Check failed",
    "unknown": "Not checked yet",
    "checking": "Checking…",
    "syncing": "Syncing with Uwazi…",
}

_DRIFT_DOT_COLORS: dict[str, str] = {
    "valid": "var(--uw-success)",
    "drifted": "var(--uw-emphasis)",
    "error": "var(--uw-emphasis)",
    "unknown": "var(--uw-ink-muted)",
    "checking": "var(--uw-warning)",
    "syncing": "var(--uw-carbon)",
}

_DRIFT_TEXT_COLORS: dict[str, str] = {
    "valid": "var(--uw-ink-tertiary)",
    "drifted": "var(--uw-emphasis)",
    "error": "var(--uw-emphasis)",
    "unknown": "var(--uw-ink-tertiary)",
    "checking": "var(--uw-ink-tertiary)",
    "syncing": "var(--uw-ink-tertiary)",
}


def _apply_drift_state(
    state: dict[str, Any],
    stored: Any,
    *,
    checking: bool = False,
    syncing: bool = False,
) -> None:
    """Paint one compact sync verdict onto the card header.

    The old red/grey banner strip above the table is now the right-hand
    "● Up to date · checked 13:40" readout. The DOT carries the verdict color
    (green = current, red = drifted/failed, grey = never checked); the text
    stays secondary grey so it does not compete with the table. The full
    sentence (and any drift detail) moves into the hover tooltip.
    """
    label = state.get("_drift_label")
    dot = state.get("_drift_icon")
    if label is None or label.is_deleted:
        return
    if syncing:
        key = "syncing"
        self_text = _DRIFT_LABELS[key]
        full_text = drift_banner_text(status="unknown", detail=None, syncing=True)
    elif checking:
        key = "checking"
        self_text = _DRIFT_LABELS[key]
        full_text = drift_banner_text(status="unknown", detail=None, checking=True)
    elif stored is None:
        key = "unknown"
        self_text = _DRIFT_LABELS[key]
        full_text = drift_banner_text(status="unknown", detail=None)
    else:
        key = str(stored.status.value)
        self_text = _DRIFT_LABELS.get(key, "Not checked yet") + drift_banner_time(stored.checked_at)
        full_text = drift_banner_text(status=key, detail=stored.detail)
    label.text = self_text
    label.style(replace=f"color: {_DRIFT_TEXT_COLORS.get(key, 'var(--uw-ink-tertiary)')};")
    if dot is not None and not dot.is_deleted:
        dot.classes(replace="sync-dot" + (" sync-dot-pulse" if key in ("checking", "syncing") else ""))
        dot.style(f"background: {_DRIFT_DOT_COLORS.get(key, 'var(--uw-ink-muted)')};")
    tooltip = state.get("_drift_tooltip")
    if tooltip is not None and not tooltip.is_deleted:
        tooltip.text = full_text
        tooltip.update()
    # Drift is the only verdict with an action attached: the table is out of
    # date, so the header offers the sync that fixes it.
    _set_drift_sync_visibility(state, stored is not None and stored.status is DriftStatus.DRIFTED and not syncing)


def _set_drift_sync_visibility(state: dict[str, Any], visible: bool) -> None:
    """Show/hide the header's Sync button (the drifted state's remedy)."""
    button = state.get("_drift_sync_button")
    if button is None or button.is_deleted:
        return
    button.set_visibility(bool(visible))


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


def _card(label: str, value: str, color: str = "primary", caption: str = "") -> None:
    """One header card: a label, the figure, and an optional second line.

    The caption carries the SECOND reading of a figure that has two (the
    Operations card counts the parts, and the entities behind them ride
    underneath), so the two levels of the table are read from one card.
    """
    with ui.card().classes("q-pa-sm").style("min-width: 118px"):
        ui.label(label).classes("text-caption text-grey-7")
        ui.label(value).classes(f"text-subtitle1 text-bold text-{color}")
        if caption:
            ui.label(caption).classes("text-caption text-grey-6")


def _change_table(state: dict[str, Any]) -> None:
    """The SUGGESTIONS table: one parent row per entity, expanded property rows.

    The parent row is the entity; the child rows are its suggested property
    values. Both levels render in the SAME table body so they share the column
    grid (a nested table inevitably drifts out of alignment). Quasar owns the
    expansion state (``props.expand``, keyed by ``row_key``) and the page owns
    the checked SUBOPERATION ids (the parent tick is derived from them), so a
    rows refresh never loses either.

    All colors, labels and derived readings arrive on the payload from Python:
    a Vue slot cannot reach ``window.*`` helpers.
    """
    from .entity_window import _revert_entity_clicked

    # The expand column holds the chevron; the checkbox is added by QTable's
    # selection support (the body slot draws it as the first cell). Fixed
    # widths keep a long suggestion from widening the table; the
    # Suggestion column takes the remaining width and clips with CSS.
    columns = [
        {"name": "expand", "label": "", "field": "expand", "align": "center", "sortable": False, "style": "width: 36px"},
        {"name": "title", "label": "Target", "field": "title", "align": "left", "sortable": True, "style": "width: 190px"},
        {
            "name": "template",
            "label": "Template",
            "field": "template",
            "align": "left",
            "sortable": True,
            "style": "width: 120px",
        },
        {
            "name": "recommendation",
            "label": "Suggestion",
            "field": "recommendation",
            "align": "left",
            "sortable": False,
            "style": "width: auto",
        },
        {"name": "ops", "label": "Operation", "field": "ops", "align": "left", "sortable": True, "style": "width: 132px"},
        {
            "name": "run_status",
            "label": "Status",
            "field": "run_status",
            "align": "left",
            "sortable": True,
            "style": "width: 176px",
        },
        {
            "name": "actions",
            "label": "Actions",
            "field": "actions",
            "align": "right",
            "sortable": False,
            "style": "width: 226px",
        },
    ]
    table = ui.table(
        rows=[],
        columns=columns,
        row_key="row_key",
        selection="multiple",
        pagination=review_sort_pagination(state.get("sort_by") or "recorded", bool(state.get("sort_desc"))),
        on_select=lambda e: _on_table_select(state, e),
    ).classes("w-full uw-table")
    table.props("flat")
    # Two clicks mean ascending then descending, never a third "unsorted"
    # state: Quasar's default cycle includes one, ``binary-state-sort`` removes
    # it. With ``rowsNumber`` (see review_sort_pagination) QTable is in server
    # mode: it draws the arrow but never re-sorts the page we hand it locally.
    table.props["binary-state-sort"] = True
    # The card footer owns "Showing a-b of N" and the page controls.
    table.props["hide-pagination"] = True
    # The expansion state lives in the prop, so the page controls it. It starts
    # empty; ``Expand all`` (if used) writes the next page's keys here.
    table.props["expanded"] = []
    # In server mode Quasar reports a header click with a ``request`` carrying
    # the new sortBy/descending (it does NOT emit ``update:sort``). Only the
    # pagination is needed; the filter and the JS cell-value helper are noise.
    table.on("request", lambda e: _table_sort_requested(state, e), ["pagination"])

    table.add_slot(
        "body",
        """
        <q-tr :props="props">
          <q-td auto-width>
            <!-- The row checkbox is SERVER-OWNED: it reads props.selected
                 (derived from the checked suboperation ids) and reports the
                 click through the table's own event. -->
            <q-checkbox v-if="!props.row.is_display_only && !props.row.is_info" :model-value="props.selected" dense
                        :disable="!props.row.has_selectable"
                        :title="props.row.rerun_blocked ? props.row.rerun_blocked_reason
                                : (props.row.has_selectable ? '' : 'Nothing to run or revert on this operation')"
                        @update:model-value="v => $parent.$emit('toggle_row', props.row.row_key, v)" />
          </q-td>
          <q-td key="expand" :props="props" class="text-center">
            <q-btn v-if="props.row.has_subs" dense flat round size="sm"
                   class="expand-btn"
                   @click.stop="props.expand = !props.expand"
                   :icon="props.expand ? 'expand_more' : 'chevron_right'">
              <q-tooltip>{{ props.expand ? 'Collapse suggestions' : 'Show suggestions' }}</q-tooltip>
            </q-btn>
          </q-td>
          <q-td key="title" :props="props">
            <div v-if="props.row.is_info" class="truncate text-body2" style="user-select: text;"
                 :title="props.row.title_full">{{ props.row.title_full }}
              <q-tooltip class="wide-tooltip">{{ props.row.title_full }}</q-tooltip>
            </div>
            <div v-else class="truncate">
              <a v-if="props.row.url" :href="props.row.url" target="_blank" rel="noopener"
                 class="entity-link" draggable="false">{{ props.row.title }}</a>
              <span v-else class="entity-link" @click="$parent.$emit('open_entity', props.row)">{{ props.row.title }}</span>
              <q-tooltip class="wide-tooltip">{{ props.row.title_full }}</q-tooltip>
            </div>
          </q-td>
          <q-td key="template" :props="props">
            <div class="truncate">{{ props.row.template || '—' }}</div>
          </q-td>
          <q-td key="recommendation" :props="props">
            <div class="truncate rec-cell">{{ props.row.recommendation || '—' }}
              <q-tooltip v-if="props.row.recommendation_full"
                         class="wide-tooltip">{{ props.row.recommendation_full }}</q-tooltip>
            </div>
          </q-td>
          <q-td key="ops" :props="props">
            <div class="row items-center no-wrap q-gutter-xs">
              <q-badge v-for="op in props.row.ops_list" :key="op" outline color="grey-8" class="op-pill">{{ op }}</q-badge>
            </div>
          </q-td>
          <q-td key="run_status" :props="props">
            <template v-if="props.row.status_text">
              <div class="row items-center no-wrap">
                <q-badge class="status-pill" :class="'status-' + props.row.status_kind">{{ props.row.status_text }}</q-badge>
              </div>
              <q-linear-progress v-if="props.row.show_progress" :value="props.row.progress" size="3px" rounded
                                 track-color="grey-3" :color="props.row.progress_color" class="status-progress q-mt-xs" />
            </template>
          </q-td>
          <q-td key="actions" :props="props" class="text-no-wrap">
            <div v-if="!props.row.is_info" class="row items-center justify-end no-wrap actions-cell">
              <q-btn v-if="!props.row.is_display_only && props.row.run_status !== 'applied'
                           && props.row.run_status !== 'discarded' && !props.row.rerun_blocked"
                     class="row-actions" outline round dense size="sm" icon="block" color="negative"
                     @click.stop="$parent.$emit('discard_op', props.row)">
                <q-tooltip>Reject these suggestions</q-tooltip>
              </q-btn>
              <q-btn v-if="props.row.is_display_only" dense flat round size="sm" icon="edit" color="teal"
                     @click.stop="$parent.$emit('open_entity', props.row)">
                <q-tooltip>Open the entity window</q-tooltip>
              </q-btn>
              <q-btn v-else class="row-actions" outline round dense size="sm" icon="edit" color="grey-8"
                     @click.stop="$parent.$emit('open_entity', props.row)">
                <q-tooltip>Edit this entity</q-tooltip>
              </q-btn>
              <q-btn v-if="!props.row.is_display_only" outline no-caps dense color="grey-9" class="apply-btn"
                     :disable="!props.row.has_runnable" @click.stop="$parent.$emit('run_op', props.row)">
                Apply
                <q-tooltip>{{ props.row.run_title }}</q-tooltip>
              </q-btn>
              <q-btn dense flat round size="sm" icon="more_horiz" color="grey-8" @click.stop>
                <q-tooltip>More actions</q-tooltip>
                <q-menu anchor="bottom right" self="top right" :offset="[0, 4]">
                  <q-list dense style="min-width: 190px">
                    <q-item clickable v-close-popup @click="$parent.$emit('open_entity', props.row)">
                      <q-item-section avatar><q-icon name="edit" /></q-item-section>
                      <q-item-section>Edit</q-item-section>
                    </q-item>
                    <q-item v-if="props.row.revertable" clickable v-close-popup
                            @click="$parent.$emit('revert_op', props.row)">
                      <q-item-section avatar><q-icon name="undo" /></q-item-section>
                      <q-item-section>Revert</q-item-section>
                    </q-item>
                    <q-item v-if="props.row.run_status === 'discarded'" clickable v-close-popup
                            @click="$parent.$emit('restore_op', props.row)">
                      <q-item-section avatar><q-icon name="restore" /></q-item-section>
                      <q-item-section>Restore</q-item-section>
                    </q-item>
                    <q-item v-else-if="!props.row.is_display_only && props.row.run_status !== 'applied'
                                       && !props.row.rerun_blocked"
                            clickable v-close-popup @click="$parent.$emit('discard_op', props.row)">
                      <q-item-section avatar><q-icon name="block" /></q-item-section>
                      <q-item-section>Reject</q-item-section>
                    </q-item>
                    <q-item v-if="props.row.url" clickable tag="a" :href="props.row.url" target="_blank">
                      <q-item-section avatar><q-icon name="open_in_new" /></q-item-section>
                      <q-item-section>Open in Uwazi</q-item-section>
                    </q-item>
                  </q-list>
                </q-menu>
              </q-btn>
            </div>
          </q-td>
        </q-tr>

        <!-- One child row per suggested property, inside the SAME table and
             with the SAME number of cells as the parent (the empty fillers
             keep every column aligned). The source paragraph gets its own row
             directly under the suggestion it explains. -->
        <template v-for="c in props.row.children" :key="c.key">
          <q-tr v-show="props.expand" class="child-row">
            <q-td auto-width>
              <q-checkbox v-if="c.selectable" dense :model-value="c.checked"
                          @update:model-value="v => $parent.$emit('toggle_sub', props.row.row_key, c.id, v)" />
              <q-icon v-else-if="!c.is_info" name="lock" size="14px" color="grey-5">
                <q-tooltip>{{ c.note || 'Not independently runnable' }}</q-tooltip>
              </q-icon>
            </q-td>
            <q-td key="expand"></q-td>
            <q-td key="title">
              <div class="child-target truncate" :title="c.target_full">{{ c.target_text }}
                <q-tooltip class="wide-tooltip">{{ c.target_full }}</q-tooltip>
              </div>
              <q-badge v-if="c.language" color="teal-9" class="lang-pill q-mt-xs">{{ c.language }}</q-badge>
            </q-td>
            <q-td key="template"></q-td>
            <q-td key="recommendation">
              <div class="rec-pair">
                <div class="rec-line rec-current">
                  <span class="rec-tag">Current</span>
                  <span class="rec-value" :title="c.current_full">{{ c.current || '—' }}
                    <q-tooltip v-if="c.current_full && c.current_full !== '—'" class="wide-tooltip">
                      {{ c.current_full }}
                    </q-tooltip>
                  </span>
                </div>
                <div class="rec-line rec-suggested">
                  <span class="rec-tag">Suggestion</span>
                  <span v-if="c.select_type && ((c.values && c.values.length) || c.extra_selection)" class="rec-values">
                    <q-chip v-for="v in c.values" :key="v.label" dense
                            :color="v.accepted ? 'green-2' : 'red-2'"
                            :text-color="v.accepted ? 'green-10' : 'red-10'"
                            :icon="v.accepted ? 'check' : 'close'">{{ v.label }}</q-chip>
                    <span v-if="c.extra_selection" class="rec-extra">{{ c.extra_selection }}</span>
                  </span>
                  <span v-else class="rec-value" :title="c.proposed_full">{{ c.proposed || '—' }}
                    <q-tooltip v-if="c.recommendation_full" class="wide-tooltip">{{ c.recommendation_full }}
                    </q-tooltip>
                  </span>
                </div>
              </div>
            </q-td>
            <q-td key="ops">
              <q-badge v-if="c.op" outline color="grey-7" class="op-pill">{{ c.op }}</q-badge>
            </q-td>
            <q-td key="run_status">
              <q-badge class="status-pill" :class="'status-' + c.status_kind">{{ c.status }}</q-badge>
            </q-td>
            <q-td key="actions" class="text-no-wrap">
              <div class="row items-center justify-end no-wrap actions-cell">
                <q-btn dense flat round size="sm" icon="more_horiz" color="grey-8" @click.stop>
                  <q-tooltip>{{ c.label || 'Suggestion actions' }}</q-tooltip>
                  <q-menu anchor="bottom right" self="top right" :offset="[0, 4]">
                    <q-list dense style="min-width: 210px">
                      <q-item v-if="c.runnable" clickable v-close-popup
                              @click="$parent.$emit('run_sub', props.row.row_key, c.id)">
                        <q-item-section avatar><q-icon name="play_arrow" /></q-item-section>
                        <q-item-section>Run this suggestion</q-item-section>
                      </q-item>
                      <q-item v-if="c.has_rejected" clickable v-close-popup
                              @click="$parent.$emit('accept_values', props.row.row_key, c.id)">
                        <q-item-section avatar><q-icon name="done_all" /></q-item-section>
                        <q-item-section>Accept every value</q-item-section>
                      </q-item>
                      <q-item v-if="c.independent && c.status === 'discarded'" clickable v-close-popup
                              @click="$parent.$emit('restore_sub', props.row.row_key, c.id)">
                        <q-item-section avatar><q-icon name="restore" /></q-item-section>
                        <q-item-section>Restore</q-item-section>
                      </q-item>
                      <q-item v-else-if="c.runnable" clickable v-close-popup
                              @click="$parent.$emit('discard_sub', props.row.row_key, c.id)">
                        <q-item-section avatar><q-icon name="block" /></q-item-section>
                        <q-item-section>Reject</q-item-section>
                      </q-item>
                      <q-item v-if="c.revertable" clickable v-close-popup
                              @click="$parent.$emit('revert_sub', props.row.row_key, c.id)">
                        <q-item-section avatar><q-icon name="undo" /></q-item-section>
                        <q-item-section>Revert this property</q-item-section>
                      </q-item>
                      <q-item v-if="!c.structural && !c.is_display_only" clickable v-close-popup
                              @click="$parent.$emit('edit_sub', props.row.row_key, c.id)">
                        <q-item-section avatar><q-icon name="edit" /></q-item-section>
                        <q-item-section>Edit in entity window</q-item-section>
                      </q-item>
                    </q-list>
                  </q-menu>
                </q-btn>
              </div>
            </q-td>
          </q-tr>
          <q-tr v-if="c.context" v-show="props.expand" class="context-row">
            <q-td></q-td>
            <q-td colspan="7">
              <div class="subs-context">{{ c.context }}</div>
            </q-td>
          </q-tr>
        </template>
        """,
    )
    table.on("run_op", lambda e: _run_op_clicked(state, _row_key_of(getattr(e, "args", None))))
    table.on("discard_op", lambda e: _discard_op_clicked(state, _row_key_of(getattr(e, "args", None))))
    table.on("restore_op", lambda e: _restore_op_clicked(state, _row_key_of(getattr(e, "args", None))))
    table.on("revert_op", lambda e: _revert_entity_clicked(state, _row_key_of(getattr(e, "args", None))))
    table.on("open_entity", lambda e: _open_entity_row(state, _row_of(e)))
    table.on("toggle_row", lambda e: _toggle_row(state, getattr(e, "args", None)))
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
    # A chevron changed the expansion: mirror it in the prop so the page's
    # expansion state stays the source of truth.
    table.on("update:expanded", lambda e: _expanded_changed(state, e))
    state["_table"] = table

    # The card footer: the result window on the left, the page controls on the
    # right. It lives INSIDE the card, under the table (the old pagination row
    # floated below it as a second, unrelated card).
    with ui.row().classes("uw-card-footer w-full items-center justify-between no-wrap"):
        state["_page_label"] = ui.label("").classes("uw-page-label")
        with ui.row().classes("items-center gap-1 no-wrap") as pager:
            state["_page_buttons"] = pager
    _reload_table(state)


def _operation_in_flight(state: dict[str, Any]) -> bool:
    """Whether this run (or any Uwazi write on the instance) is in progress.

    A Run/Revert is WRITING Uwazi, so a drift probe would read the half-applied
    state and paint "Out of date" because of the operation itself. The same
    holds for this run's sync (a ``refresh`` claim): the table is being re-read
    and the banner already says so. Drift checks resume as soon as the claim
    (and the write gate) is released.
    """
    instance = state.get("instance_url", "")
    run_id = state.get("run_id", "")
    if run_claim(instance, run_id) is not None:
        return True
    return snapshot(instance) is not None


async def _check_drift_now(state: dict[str, Any], notify: bool = True, client: Any = None) -> None:
    """Run the drift check in the background thread and repaint the banner.

    ``client`` is captured by the caller INSIDE the page's slot context (the
    same pattern ``_run_rows`` uses): a background task starts with an empty
    slot stack, so every UI call it makes — the banner updates and the
    toasts — must re-enter the client explicitly via ``with client:``.
    """
    if _operation_in_flight(state):
        # Never probe during a write/sync: the drift would be our own doing.
        return
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
    if _operation_in_flight(state):
        # A write/sync began while the probe ran: its verdict describes a
        # half-applied state, so it must not repaint the banner.
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
    state["_global_applied_ids"] = set()


def _clear_selection(state: dict[str, Any], *, clear_page: bool) -> None:
    """Leave whole-result mode and (optionally) uncheck the visible page."""
    state["_select_all"] = False
    state["_global_refs"] = {}
    state["_global_applied_ids"] = set()
    if clear_page:
        state["_checked"] = set()
    _sync_selection(state)


def _current_filter(
    state: dict[str, Any],
    *,
    limit: int = REVIEW_PAGE_SIZE,
) -> SuggestionFilter:
    return filter_from_controls(
        status=state["status"],
        op=state["op"],
        property_name=state["property"],
        language=state["language"],
        text=state["text"],
        offset=state["offset"],
        limit=limit,
        sort_by=state["sort_by"],
        sort_desc=state["sort_desc"],
    )


def _discard_from_state(state: dict[str, Any]) -> None:
    """Open the bulk-Discard dialog for the runnable checked SUBOPERATIONS.

    The dialog asks for the reason too: the operator discards an operation
    because the end user does not want it executed, and WHY is worth keeping
    with the rows (it is stored as the rows' discard reason). Only parts still
    to be written are discarded — an applied row is undone by Revert, not
    refused.
    """
    refs = _checked_flags(state, "run")
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

    noun = "suggestion" if len(ids) == 1 else "suggestions"
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
        count = await asyncio.to_thread(discard_ids, state["instance_url"], state["run_id"], ids, reason)
        with client:
            ui.notify(
                f"Discarded {count} suggestion(s) — a Run will not execute them",
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
    """Mark ONE entity's visible suggestions as discarded.

    The filter is a SUGGESTION filter, so "the operation" here means the parts
    the list is showing: discarding an entity under a property filter refuses
    those property suggestions, not the ones the filter hid.
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
        "Mark this suggestion as discarded? A Run will not apply it. The reason is stored with it.",
        _submit,
        title="Discard suggestions",
        confirm_label="Discard",
        reason_label="Reason (optional — why the end user does not want this applied)",
    )


def _discard_sub_clicked(state: dict[str, Any], args: Any) -> None:
    """Discard ONE suggestion (its id is the whole selection)."""
    _row, sub = _sub_of(state, args)
    if sub is None or sub.get("is_info") or sub.get("is_display_only") or not isinstance(sub.get("id"), int):
        return
    sub_id = int(sub["id"])
    client = context.client
    background_tasks.create(
        _discard_ids(state, [sub_id], "discarded from the suggestions list", client),
        name=f"discard sub {sub_id}",
    )


def _drift_banner(state: dict[str, Any]) -> None:
    """The card header's sync readout: "● Up to date · checked 13:40".

    It renders the STORED verdict immediately and ALWAYS starts a fresh check:
    entering the page is the moment the operator needs to know whether the
    list still matches Uwazi. The drifted state grows a small Sync button at
    the end of the same line — the verdict's remedy next to the warning.
    """
    with ui.row().classes("items-center gap-2 no-wrap sync-status"):
        state["_drift_icon"] = ui.element("span").classes("sync-dot")
        state["_drift_label"] = ui.label(_DRIFT_LABELS["unknown"]).classes("sync-label")
        with state["_drift_label"]:
            state["_drift_tooltip"] = ui.tooltip("")
        state["_drift_sync_button"] = (
            ui.button(
                "Sync with Uwazi",
                icon="sync",
                on_click=lambda: _sync_now_clicked(state),
            )
            .props("unelevated no-caps color=primary size=md")
            .classes("sync-button")
        )
        state["_drift_sync_button"].set_visibility(False)

    stored = get_drift_state(page_instance_url(), state["run_id"])
    in_flight = _operation_in_flight(state)
    _apply_drift_state(state, stored, checking=stored is None and not in_flight)
    # EVERY visit checks drift: Uwazi moves between visits, so a stored verdict
    # (even "up to date") describes the moment it was taken, not now. The check
    # runs in the background — the page renders the stored verdict meanwhile.
    if not in_flight:
        background_tasks.create(_check_drift_now(state, notify=False, client=context.client))


def _sync_now_clicked(state: dict[str, Any]) -> None:
    """The banner's Sync button (defined in ``page``: ``core`` cannot import it)."""
    from .page import _update_table_clicked

    _update_table_clicked(state)


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
        # A per-row toggle replaces the page-wide mode: "mixed" is not carried
        # to the next page, only a uniform Expand all / Collapse all is.
        state["_expand_mode"] = expansion_mode(table.rows or [], state["_expanded"])
        table.update()
    _refresh_expand_button(state)


def _modal_header(dialog: Any, title: str) -> None:
    """Modal title row with a top-right Close button (matches task_dialogs)."""
    with ui.row().classes("uw-modal-header"):
        ui.label(title).classes("uw-modal-title")
        ui.button(icon="close", on_click=dialog.close).props("flat round dense").classes("uw-modal-close").tooltip("Close")


def _on_table_select(state: dict[str, Any], event: Any) -> None:
    """The table's selection callback: only the counters need refreshing.

    The cascade to the suboperations lives in :func:`_selection_changed`, which
    listens to the raw event carrying WHICH rows were added or removed.
    """
    _refresh_selection_counts(state)


def _open_entity_row(state: dict[str, Any], row: dict[str, Any]) -> None:
    """Open the entity window for one operation row (its first written language).

    Every row that names an entity opens — including a validation row and a
    not-processed/matched one. Those rows carry no work to write, but the
    operator still needs the evidence behind them: the entity's document (often
    the PDF the value was extracted from) and the row's recorded values. Info
    rows name no entity, so they still open nothing.
    """
    from .entity_window import _open_entity_window

    if row.get("is_info"):
        return
    shared_id = str(row.get("shared_id") or "")
    if not shared_id:
        return
    languages = [lang for lang in (row.get("languages") or []) if lang]
    # A created entity's row is keyed by its placeholder; its language group is
    # the language-independent one (a create writes no per-language row).
    if any(op in CREATE_OPS for op in str(row.get("ops") or "").split(", ") if op):
        _open_entity_window(state, shared_id, None)
        return
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
        _repaint_drift_from_store(state)
        if state["offset"] >= state["total"] and state["offset"] > 0:
            state["offset"] = (_page_count(state) - 1) * REVIEW_PAGE_SIZE
        _reload_table(state)
    except Exception:  # noqa: BLE001 — a refresh failure must not break the page
        logger.exception("review refresh failed")


def _repaint_drift_from_store(state: dict[str, Any]) -> None:
    """Repaint the drift banner from the STORED verdict (local read, no network).

    A Run/Revert now re-syncs the baseline and records a VALID verdict, but the
    banner was only ever repainted by a drift CHECK — so the page kept the red
    "Out of date" line the check painted before the write, right after the write
    that fixed it. Reading the verdict back and applying it keeps the banner
    honest without paying a probe per refresh.
    """
    try:
        stored = get_drift_state(state["instance_url"], state["run_id"])
    except Exception as exc:  # noqa: BLE001 — the banner is display state, never an error path
        logger.debug("could not read the drift verdict for {}: {}", state["run_id"], exc)
        return
    _apply_drift_state(state, stored)


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
    """Rebuild every filter dropdown from the FACETED counts (best-effort).

    Each dropdown's options are counted with the OTHER active filters applied:
    narrowing the table by property must narrow the Operation/Status numbers
    too, and an option that can only return zero rows is not offered. Each
    dimension is counted with ITS OWN filter cleared, so the current choice
    never cancels out its own options.

    The counts move when rows change status (accept, discard, run, revert), so
    the controls are rebuilt after every refresh. ``set_options`` keeps the
    current selection, and the builders keep a selected value listed even at
    ``(0)``, so a filter is never silently cleared.
    """
    try:
        counts = get_filter_counts(page_instance_url(), state["run_id"], _current_filter(state, limit=100000))
    except Exception:  # noqa: BLE001 — labels must not break a table refresh
        logger.exception("could not refresh the filter counts")
        return
    for key, labels in (("op", OP_LABELS), ("status", _STATUS_LABELS)):
        widget = state.get(f"_{key}_select")
        if widget is None or widget.is_deleted:
            continue
        widget.set_options(filter_options(labels, counts.get(key, {}), state[key]))
    property_widget = state.get("_property_select")
    if property_widget is not None and not property_widget.is_deleted:
        property_widget.set_options(
            property_filter_options(state.get("_labels") or {}, counts.get("property", {}), state["property"])
        )
    language_widget = state.get("_language_select")
    if language_widget is not None and not language_widget.is_deleted:
        language_widget.set_options(language_filter_options(counts.get("language", {}), state["language"]))


def _page_window(current: int, pages: int) -> list[int]:
    """The page numbers the footer shows around the current page (pure).

    Five slots, kept inside the result set: the target's compact "‹ 1 2 3 ›"
    pager, generalized to long runs without a run of hundreds of buttons.
    """
    if pages <= 5:
        return list(range(1, pages + 1))
    start = max(1, min(current - 2, pages - 4))
    return list(range(start, start + 5))


def _refresh_page_controls(state: dict[str, Any]) -> None:
    """Rebuild the footer's page buttons around the current page.

    The pager offers the first and last page directly plus a typed page
    number, so a run of thousands of pages never forces a walk through the
    five visible buttons.
    """
    holder = state.get("_page_buttons")
    if holder is None or holder.is_deleted:
        return
    pages = _page_count(state)
    page = state["offset"] // REVIEW_PAGE_SIZE + 1
    holder.clear()
    with holder:
        first = ui.button(icon="first_page", on_click=lambda: _page_to(state, 1)).props("flat dense round")
        first.set_enabled(page > 1)
        previous = ui.button(icon="chevron_left", on_click=lambda: _page(state, -1)).props("flat dense round")
        previous.set_enabled(page > 1)
        for number in _page_window(page, pages):
            button = ui.button(str(number), on_click=lambda n=number: _page_to(state, n)).props("flat dense no-caps")
            button.classes("page-btn")
            if number == page:
                button.classes(add="page-btn-active")
        following = ui.button(icon="chevron_right", on_click=lambda: _page(state, 1)).props("flat dense round")
        following.set_enabled(page < pages)
        last = ui.button(icon="last_page", on_click=lambda: _page_to(state, None)).props("flat dense round")
        last.set_enabled(page < pages)
        ui.label("Go to").classes("uw-page-label")
        page_input = (
            ui.number(value=page, min=1, max=pages, step=1)
            .props("dense outlined hide-bottom-space")
            .classes("page-input")
            .tooltip(f"Page number (1–{pages})")
        )
        page_input.on("keydown.enter", lambda _e: _page_from_input(state, page_input.value))
        state["_page_input"] = page_input
        ui.button(icon="arrow_forward", on_click=lambda: _page_from_input(state, page_input.value)).props(
            "flat dense round"
        ).tooltip("Go to this page")
        ui.label(f"/ {pages}").classes("uw-page-label")


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


def _checked_flags(state: dict[str, Any], kind: str) -> list[tuple[int, str, str | None, str]]:
    """The checked refs whose part can be acted on by ``kind`` (pure over state).

    The payload carries WHY a part is selectable (``run_selectable`` for Run and
    Discard, ``revert_selectable`` for Revert) because a tick means different
    things to the three buttons. In whole-result mode the projection is a plain
    ref list with no flags, so those refs count for ``run`` only — "select all
    results" selects runnable rows by construction.
    """
    rows = state.get("_table")
    flagged: dict[int, dict[str, Any]] = {}
    if rows is not None and not rows.is_deleted:
        for row in rows.rows or []:
            if not isinstance(row, dict):
                continue
            for sub in row.get("subs") or []:
                if isinstance(sub, dict) and isinstance(sub.get("id"), int):
                    flagged[int(sub["id"])] = sub
    checked = state.get("_checked") or set()
    refs = {ref[0]: ref for ref in (_checked_refs(state))}
    # In whole-result mode a checked id may not be on the current page, so its
    # flags are gone. The projection's own split says which side it belongs to:
    # ``_global_applied_ids`` is what "select all" added for REVERT.
    applied_ids = state.get("_global_applied_ids") or set()
    selected: list[tuple[int, str, str | None, str]] = []
    for sub_id in checked:
        ref = refs.get(sub_id)
        if ref is None:
            continue
        sub = flagged.get(sub_id)
        if sub is None:
            # Whole-result mode (or another page): no payload flags. A row the
            # projection reported as runnable belongs to both sides; one it
            # reported as applied belongs to REVERT only.
            if kind == "run" and sub_id not in applied_ids:
                selected.append(ref)
            elif kind == "revert":
                selected.append(ref)
            continue
        if sub.get(kind + "_selectable") or (kind == "run" and sub.get("runnable")):
            # A LOCKED part (a delete's whole-entity write) has no checkbox of
            # its own, but the operation's tick selects it and its Run writes
            # it. ``runnable`` is what makes those parts count for Run, exactly
            # as ``_set_children_checked`` selected them.
            selected.append(ref)
    return selected


def _refresh_selection_counts(state: dict[str, Any]) -> None:
    """Update the selection-driven buttons' texts from the checked SUBOPERATIONS.

    ONE selection, THREE meanings — each button counts the checked parts IT can
    act on, so a selection of applied rows reads "Revert (N)" and "Run (0)":

    - Run/Discard act on the parts still to be written (pending/reverted), and
      an applied or discarded part must never inflate them;
    - Revert acts on the APPLIED parts' distinct entities — the top Revert
      could not be used before, because an applied row had no checkbox at all
      (the row checkbox was disabled on "nothing runnable").

    Both counts collapse to DISTINCT entities for Revert (one backup covers
    every language of an entity, so the entity reverts once).
    """
    run_refs = _checked_flags(state, "run")
    revert_refs = _checked_flags(state, "revert")
    run_count = len(run_refs)
    entities = len(entities_of_refs(revert_refs))
    selection = _checked_refs(state)
    selected_entities = len(entities_of_refs(selection))
    selected_label = state.get("_selection_label")
    if selected_label is not None and not selected_label.is_deleted:
        selected_label.text = f"{selected_entities:,} selected"
    # The bar is always present; only its buttons depend on what is checked.
    run_button = state.get("_run_button")
    if run_button is not None and not run_button.is_deleted:
        run_button.text = run_button_label(run_count)
        run_button.set_enabled(run_count > 0)
    revert_button = state.get("_bulk_revert_button")
    if revert_button is not None and not revert_button.is_deleted:
        # The target shows the verb alone; the count lives in the selected
        # label and in the Run button (the one action whose scope is the row
        # count the operator just selected).
        revert_button.text = "Revert"
        revert_button.set_enabled(entities > 0)
    discard_button = state.get("_bulk_discard_button")
    if discard_button is not None and not discard_button.is_deleted:
        discard_button.text = "Discard"
        discard_button.set_enabled(run_count > 0)
    select_button = state.get("_select_all_button")
    if select_button is not None and not select_button.is_deleted:
        total = int(state.get("total") or 0)
        all_selected = bool(state.get("_select_all"))
        select_button.text = select_all_label(total, all_selected)
        select_button.set_enabled(all_selected or total > 0)


def _sync_table_sort(state: dict[str, Any]) -> None:
    """Mirror the server-side sort into the table's pagination (and its arrows).

    The state dict is the one source of truth; Quasar only displays the sort
    arrow. Keeping the pagination in step here is what lets the NEXT header
    click toggle the right direction, and what keeps the arrow honest after a
    reload changes the row count.
    """
    table = state.get("_table")
    if table is None or table.is_deleted:
        return
    pagination = review_sort_pagination(
        state.get("sort_by") or "recorded",
        bool(state.get("sort_desc")),
        int(state.get("total") or 0),
    )
    # A running pass reloads the table every few seconds: only push the prop
    # when the sort or the row count actually changed.
    if table.pagination != pagination:
        table.pagination = pagination


def _table_sort_requested(state: dict[str, Any], event: Any) -> None:
    """A header click: translate Quasar's column name and re-query in that order.

    Quasar has already flipped its own arrow, but the ORDER of the rows comes
    from SQL, not from the browser: the whole filtered set must be re-ordered
    before the page window is taken. Requests that do not carry a known column
    (a page/filter change, an unknown name) leave the current sort alone.
    """
    args = getattr(event, "args", None)
    # With ``args=['pagination']`` NiceGUI forwards the ONE event object as
    # ``{'pagination': {...}}`` (a bare dict when no key filter is given).
    pagination = args.get("pagination") if isinstance(args, dict) else None
    if not isinstance(pagination, dict):
        return
    column = str(pagination.get("sortBy") or "")
    sort_by = state.get("sort_by") or "recorded"
    descending = bool(state.get("sort_desc"))
    ordered = sort_from_header_click(sort_by, descending, column)
    if ordered is None:
        return
    new_by, new_desc = ordered
    if new_by == sort_by and new_desc == descending:
        return
    state["sort_by"] = new_by
    state["sort_desc"] = new_desc
    state["offset"] = 0
    _reload_table(state)


def _clip_text(text: str, limit: int) -> str:
    """Cut a display string to one line, with the ellipsis the cells use (pure)."""
    return text if len(text) <= limit else text[:limit] + "…"


_SUB_STATUS_KINDS: dict[str, str] = {
    "applied": "applied",
    "pending": "pending",
    "reverted": "pending",
    "discarded": "discarded",
    "match": "applied",
    "matched": "applied",
    "mismatch": "negative",
    "excluded": "neutral",
    "skipped": "neutral",
    "info": "neutral",
    "n/a": "neutral",
}


def _decorate_operation_payload(payload: dict[str, Any], item: Any) -> None:
    """Add the presentation fields the suggestions table's slots read (pure).

    The payload from :func:`operation_row` already carries every domain fact;
    this adds only what is specific to the new table: the collapsed row's
    RECOMMENDATION (derived from its property parts), the "X of Y applied"
    reading with its progress, and one presentation contract per child row.
    Keeping it here means the Vue slot stays a plain property read.
    """
    subs = [sub for sub in (payload.get("subs") or []) if isinstance(sub, dict)]
    total = int(getattr(item, "proposal_count", 0) or 0)
    applied = int(getattr(item, "applied", 0) or 0)
    discarded = int(getattr(item, "discarded", 0) or 0)
    payload["applied_count"] = applied
    payload["total_count"] = total
    payload["discarded_count"] = discarded
    payload["all_discarded"] = bool(total and discarded >= total)
    payload["progress"] = (applied / total) if total else 0.0
    payload["progress_color"] = "positive" if total and applied >= total else "warning"
    recommendation = recommendation_text(subs)
    payload["recommendation_full"] = recommendation
    payload["recommendation"] = _clip_text(recommendation, 160)
    payload["ops_list"] = [op for op in str(payload.get("ops") or "").split(", ") if op]
    # The slot renders the parent and then one sibling `<q-tr>` per entry, so
    # both levels share the table's column grid.
    payload["children"] = subs

    seen_keys: set[str] = set()
    for index, sub in enumerate(subs):
        # Give every child a unique Vue key: an info/structural row may share
        # its suggestion id with a sibling (or have none), and duplicate keys
        # would make Vue reuse the wrong row.
        key = str(sub.get("key") or f"sub-{index}")
        if key in seen_keys:
            key = f"{key}-{index}"
        seen_keys.add(key)
        sub["key"] = key
        label = str(sub.get("label") or "").strip()
        proposed = str(sub.get("proposed") or "").strip()
        proposed_full = str(sub.get("proposed_full") or proposed).strip()
        if label and proposed and proposed != "—":
            sub["target_text"] = f"{label} → {proposed}"
        else:
            sub["target_text"] = label or proposed or "—"
        sub["target_full"] = (
            f"{label} → {proposed_full}" if label and proposed_full and proposed_full != "—" else (label or proposed_full)
        )
        sub["recommendation"] = "" if proposed == "—" else _clip_text(proposed_full, 160)
        sub["recommendation_full"] = "" if proposed_full == "—" else proposed_full
        sub["status_kind"] = _SUB_STATUS_KINDS.get(str(sub.get("status") or ""), "neutral")
        sub["language"] = str(sub.get("language") or "")

    is_info = bool(payload.get("is_info"))
    is_display_only = bool(payload.get("is_display_only"))
    run_status = str(payload.get("run_status") or "pending")
    if is_info:
        payload["status_text"] = ""
        payload["status_kind"] = "neutral"
        payload["show_progress"] = False
    elif is_display_only:
        payload["status_text"] = run_status
        payload["status_kind"] = _SUB_STATUS_KINDS.get(run_status, "neutral")
        payload["show_progress"] = False
    elif payload["all_discarded"]:
        payload["status_text"] = "Discarded"
        payload["status_kind"] = "discarded"
        payload["show_progress"] = False
    elif total:
        payload["status_text"] = f"{applied} of {total} applied"
        payload["status_kind"] = "applied" if applied >= total else "pending"
        payload["show_progress"] = True
    else:
        payload["status_text"] = run_status
        payload["status_kind"] = "neutral"
        payload["show_progress"] = False


def _reload_table(state: dict[str, Any]) -> None:
    """Load one page of OPERATIONS with their suboperations and update the table.

    The filter is a SUBOPERATION filter, so an operation whose every part the
    filter removed never reaches the page — "no suboperations left means no
    row", enforced by the store. The rows carry each entity's public Uwazi URL
    (read once here, so the table costs no extra request for it).
    """
    from .entity_window import _labels_for_operation, _types_for_operation
    from .value_pickers import relationship_titles

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
        labels_for = {id(item): _labels_for_operation(item, labels_by_template, state.get("_labels") or {}) for item in rows}
        types_for = {id(item): _types_for_operation(item, types_by_template, state.get("_types") or {}) for item in rows}
        # Relationship values store TARGET sharedIds; the table shows titles.
        # One lookup for every relationship row of the page (cached per
        # instance+language), so a page costs at most one batch of id reads.
        relationship_rows = [
            sub
            for item in rows
            for sub in item.subs
            if types_for[id(item)].get(sub.property_name or "") == RELATIONSHIP_TYPE
        ]
        titles = relationship_titles(relationship_rows) if relationship_rows else {}
        payloads = [
            operation_row(
                item,
                checked_ids=state.get("_checked") or (),
                uwazi_url=base_url,
                labels=labels_for[id(item)],
                types=types_for[id(item)],
                titles=titles,
            )
            for item in rows
        ]
        for item, payload in zip(rows, payloads, strict=True):
            _decorate_operation_payload(payload, item)
        # The Revert icon names the backup it would restore, the way the
        # entity table's did: the date is already read for the confirm dialog.
        # A row the run CREATED has no backup: its undo deletes the entity, and
        # the tooltip says so instead of promising a restore.
        for payload in payloads:
            if any(op in CREATE_OPS for op in payload.get("ops", "").split(", ") if op):
                payload["revert_tooltip"] = "Delete this entity — the run created it, so undoing the creation removes it"
                continue
            captured = tooltips.get(payload["shared_id"])
            payload["revert_tooltip"] = f"entity backup creation {captured:%Y-%m-%d %H:%M}" if captured is not None else ""
        table = state.get("_table")
        if table is not None and not table.is_deleted:
            table.rows = payloads
            # The default header checkbox selects the current PAGE. When
            # nothing on the page (or at all: an empty result) is selectable,
            # it is dead weight and it does nothing — hide it (see the CSS on
            # the review page).
            if any(isinstance(payload, dict) and payload.get("has_selectable") for payload in payloads):
                table.classes(remove="review-no-selectable")
            else:
                table.classes(add="review-no-selectable")
            # The page-wide expansion (Expand all / Collapse all) is a MODE, not
            # a list of the current page's keys: applying it here is what makes
            # the next page open with the same rows expanded (or collapsed).
            state["_expanded"] = expanded_keys_for(
                payloads,
                state.get("_expand_mode", "none"),
                state.get("_expanded") or set(),
            )
            table.props["expanded"] = list(state["_expanded"])
            if not state.get("_select_all"):
                # Page selection: drop checked ids that are no longer visible,
                # so the count (and Run) describe what the operator can see.
                visible = {
                    int(sub["id"])
                    for payload in payloads
                    for sub in payload.get("subs") or []
                    if isinstance(sub.get("id"), int)
                    and (sub.get("selectable") or sub.get("runnable") or sub.get("revert_selectable"))
                }
                state["_checked"] = {sub_id for sub_id in (state.get("_checked") or ()) if sub_id in visible}
            _sync_selection(state)
        label = state.get("_page_label")
        if label is not None and not label.is_deleted:
            first = state["offset"] + 1 if total else 0
            last = min(state["offset"] + REVIEW_PAGE_SIZE, total)
            label.text = f"Showing {first:,}–{last:,} of {total:,}"
        _refresh_page_controls(state)
        _sync_table_sort(state)
        _refresh_expand_button(state)
        # The summary reads "N suggestions · M entities" over the FILTERED set,
        # so it is repainted with the same totals the table just received.
        _render_summary(state)
    finally:
        state["loading"] = False
        # Every table load re-checks drift: the table is a snapshot of Uwazi, so
        # a reload (a filter change, a page flip, a post-action refresh) is a
        # moment the operator needs to know whether it still matches. The check
        # runs in the background and repaints the banner in place. While a pass
        # is running the table reloads every few seconds, so a short throttle
        # keeps one check per interval instead of one per poll.
        _schedule_drift_check(state)


def _schedule_drift_check(state: dict[str, Any]) -> None:
    """Start a drift check unless one just ran (the table-load hook)."""
    if state.get("_drift_label") is None:
        return
    if _operation_in_flight(state):
        # A Run/Revert or a sync is writing/re-reading Uwazi: the probe would
        # report our own work as drift and paint the banner red mid-pass.
        return
    now = time.monotonic()
    last = float(state.get("_drift_checked_at") or 0.0)
    if now - last < 10.0:
        return
    state["_drift_checked_at"] = now
    background_tasks.create(
        _check_drift_now(state, notify=False, client=context.client),
        name=f"drift {state['run_id']}",
    )


def _render_summary(state: dict[str, Any]) -> None:
    """Paint the card's summary over the CURRENT filter (pure-ish).

    "N suggestions · M entities" has to describe the filtered list, not the
    run's absolute totals: the entity count is the table's own total, and the
    suggestion count is the same op facet the Operation filter uses (summed
    over the selected operations, or over all of them). Counting through one
    existing faceted query avoids loading every row just for a number.
    """
    # The performance line lives OUTSIDE this holder (always visible, at the top
    # of the page) but it is refreshed here so the score and the summary are
    # never painted from different moments.
    _render_performance(state)
    label = state.get("_summary_label")
    if label is None or label.is_deleted:
        return
    try:
        counts = get_filter_counts(page_instance_url(), state["run_id"], _current_filter(state, limit=100000))
    except Exception:  # noqa: BLE001 — a summary failure must not break the table
        logger.exception("could not count the filtered suggestions")
        counts = {}
    op_counts = counts.get("op", {})
    selected_ops = [str(op) for op in (state.get("op") or [])]
    suggestions = sum(op_counts.get(op, 0) for op in selected_ops) if selected_ops else sum(op_counts.values())
    entities = int(state.get("total") or 0)
    label.text = f"{suggestions:,} suggestions · {entities:,} entities"


def _reset_global_selection(state: dict[str, Any]) -> None:
    """Drop a whole-result selection when the filter that defined it changes."""
    if state.get("_select_all"):
        _clear_selection(state, clear_page=True)


async def _restore_ids(state: dict[str, Any], ids: list[int], client: Any) -> None:
    """Return the given SUBOPERATIONS' discarded rows to pending, then refresh."""
    try:
        count = await asyncio.to_thread(restore_ids, state["instance_url"], state["run_id"], ids)
        with client:
            ui.notify(f"Restored {count} suggestion(s) — they can be run again", type="positive")
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
    """Open the bulk-Revert confirmation for the selection the button's count describes.

    The entities come from the REVERT side of the selection: the checked parts
    whose write exists to undo. A pending part of an applied entity cannot ride
    a revert (there is nothing to restore for it), and an applied part must not
    be dropped just because its siblings are still pending.
    """
    entities = entities_of_refs(_checked_flags(state, "revert"))
    if not entities:
        ui.notify("Nothing selected to revert", type="warning")
        return

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
        "only the checked entities are reverted. Entities the run CREATED have no backup — they are "
        "DELETED instead (their rows go back to pending). Entities the run DELETED are re-created from "
        "their backup with a NEW sharedId (the old one cannot be reused)."
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


def holdout_warning_message(count: int) -> str:
    """The sentence the Run confirmation appends when a held-out entity would change (pure).

    Validation rows themselves cannot be run, so this fires only for a
    REGULAR row of an entity that is ALSO in the set (a promoted sibling, or a
    future surface that forgot the guard). Saying it in the confirmation is the
    last line of defence before Uwazi is written.
    """
    entities = "entity" if count == 1 else "entities"
    return (
        f"WARNING: {count} {entities} in this selection belong to the validation set. "
        "Writing them changes your test data, and the performance score stops being a fair estimate."
    )


def _holdout_pairs_in_selection(state: dict[str, Any], ids: list[int]) -> set[tuple[str, str | None]]:
    """The held-out (entity, language) rows the selection would touch (pure over state).

    With validation rows never selectable this should be empty; it exists so a
    future surface cannot silently write a held-out entity, and the confirmation
    says so when it happens.
    """
    try:
        summary = validation_summary(state["instance_url"], state["run_id"])
    except Exception:  # noqa: BLE001 — a warning must never block a Run
        return set()
    validation = summary.get("set")
    if validation is None:
        return set()
    held = set(validation.shared_ids)
    refs = {ref[0]: ref for ref in _checked_refs(state)}
    return {(ref[1], ref[2]) for ref in refs.values() if ref[0] in ids and ref[1] in held}


def _performance_row(state: dict[str, Any]) -> None:
    """The dismissible performance banner above the card.

    A run with no test set starts with the banner HIDDEN: there is no score to
    show, and the empty strip took the top of the page from the table. The
    Settings menu's "Show performance" brings it back (dismissing it hides it
    again). A scored run shows the same figure the old always-on line carried,
    in the same strip, so the page's top stays one calm sentence instead of a
    stack of counter cards.
    """
    banner = ui.element("div").classes("uw-info-banner w-full")
    state["_performance_banner"] = banner
    with banner:
        with ui.row().classes("w-full items-center justify-between no-wrap"):
            holder = ui.row().classes("items-center gap-2 no-wrap grow")
            state["_performance_holder"] = holder
            ui.button(icon="close", on_click=lambda: _dismiss_performance(state)).props(
                "flat dense round color=grey-8"
            ).classes("uw-banner-close").tooltip("Dismiss")
    _render_performance(state)


def _show_performance(state: dict[str, Any]) -> None:
    """Bring a dismissed (or test-set-less) performance banner back."""
    state["_performance_dismissed"] = False
    state["_performance_forced"] = True
    _render_performance(state)


def _dismiss_performance(state: dict[str, Any]) -> None:
    """Hide the performance banner until the Settings menu shows it again."""
    state["_performance_dismissed"] = True
    state["_performance_forced"] = False
    banner = state.get("_performance_banner")
    if banner is not None and not banner.is_deleted:
        banner.set_visibility(False)


def _render_performance(state: dict[str, Any]) -> None:
    """Paint the performance banner (called on load and after every refresh)."""
    holder = state.get("_performance_holder")
    banner = state.get("_performance_banner")
    if holder is None or holder.is_deleted or banner is None or banner.is_deleted:
        return
    if state.get("_performance_dismissed"):
        banner.set_visibility(False)
        return
    try:
        summary = validation_summary(state["instance_url"], state["run_id"])
    except Exception as exc:  # noqa: BLE001 — the line degrades, the page never breaks
        logger.warning("validation banner failed: {}", exc)
        banner.set_visibility(False)
        return
    validation = summary.get("set")
    if validation is None and not state.get("_performance_forced"):
        # No test set means no measurement: the table keeps the top of the page
        # until the operator asks for the banner from the Settings menu.
        banner.set_visibility(False)
        return
    banner.set_visibility(True)
    holder.clear()
    with holder:
        ui.icon("info").classes("uw-banner-icon")
        if validation is None:
            with ui.row().classes("items-baseline gap-1 no-wrap"):
                ui.label("Performance —").classes("uw-banner-title")
                ui.label("No test set. Add a test set in the wizard to measure this task.").classes("uw-banner-text")
            return
        if not performance_visible(summary):
            with ui.row().classes("items-baseline gap-1 no-wrap"):
                ui.label("Performance —").classes("uw-banner-title")
                ui.label("The test set is recorded but has not been scored yet.").classes("uw-banner-text")
            return
        score = summary.get("score")
        state_word = {
            "fresh": "Unseen test set",
            "pending": "Test set ready — not scored yet",
            "stale": "Test set changed — score out of date",
            "contaminated": "Used for fixes — regression score only",
        }.get(summary["state"], summary["state"])
        color = "teal" if summary["state"] == "fresh" else "orange"
        label = _validation_property_text(state, validation.property_name)
        with ui.row().classes("items-baseline gap-1 no-wrap"):
            ui.label("Performance —").classes("uw-banner-title")
            ui.label(f"{label} ·").classes("uw-banner-text")
        if score is None:
            ui.label("—").classes("text-subtitle2 text-bold text-grey-6")
        else:
            ui.label(score.value_text()).classes("text-subtitle1 text-bold uw-banner-value")
            if score.detail():
                ui.label(score.detail()).classes("text-caption text-grey-6")
        ui.badge(state_word).props(f"color={color}").classes("q-ml-sm")


def _validation_property_text(state: dict[str, Any], property_name: str) -> str:
    """The measured property's display name, from the run's label map (pure-ish)."""
    labels = state.get("_labels") or {}
    return labels.get(property_name) or ("Title" if property_name == "_title" else property_name)


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
    """Open the Run confirmation for the RUNNABLE side of the selection.

    The selection may also carry applied rows (checked so the top Revert can
    address them); Run writes only the parts still to be written, so it reads
    the runnable subset and never re-writes a landed change.
    """
    refs = _checked_flags(state, "run")
    _run_refs(state, refs, ops_of_refs(refs))


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
        ui.notify("This entity has nothing to run", type="warning")
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
        ui.notify("Nothing left to run for this entity", type="warning")
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
    holdout = _holdout_pairs_in_selection(state, ids)
    if holdout:
        message = f"{message} {holdout_warning_message(len(holdout))}"
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


def _set_children_checked(state: dict[str, Any], row: dict[str, Any], checked: bool) -> None:
    """Tick/untick every part of one operation.

    A tick is a SELECTION, not an acceptance: the parts' statuses are left
    alone. Locked parts are included because the operation's Run covers them —
    they simply have no checkbox of their own to show it. Info rows are
    display-only and carry no selection at all.
    """
    if row.get("is_display_only") or row.get("is_info"):
        return
    # Every part an action can address enters the selection: the ones a Run may
    # still write, and the APPLIED ones a Revert restores. A discarded part
    # (restore first) and a validation part stay out. Run/Discard only count
    # the runnable ones, Revert only the applied ones (see
    # ``_refresh_selection_counts``); a tick is a selection, not an action.
    ids = [
        int(sub["id"])
        for sub in (row.get("subs") or [])
        if isinstance(sub, dict)
        and (sub.get("runnable") or sub.get("revert_selectable"))
        and not sub.get("is_validation")
        and isinstance(sub.get("id"), int)
    ]
    _apply_checked(state, ids, checked)


def _set_drift_error(state: dict[str, Any], text: str) -> None:
    """Write a failed-check verdict onto the header readout (pure-ish).

    The check failed, so the dot goes red and the full failure is available in
    the tooltip; the text stays the two-word verdict the header has room for.
    """
    label = state.get("_drift_label")
    if label is None or label.is_deleted:
        return
    label.text = "Check failed"
    label.style(replace="color: var(--uw-emphasis);")
    dot = state.get("_drift_icon")
    if dot is not None and not dot.is_deleted:
        dot.classes(replace="sync-dot")
        dot.style("background: var(--uw-emphasis);")
    tooltip = state.get("_drift_tooltip")
    if tooltip is not None and not tooltip.is_deleted:
        tooltip.text = text
        tooltip.update()
    # A failed check proves nothing about the table's currency, so the sync
    # action stays hidden until a check actually reports drift.
    _set_drift_sync_visibility(state, False)


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
        and not suggestion.is_validation
        and suggestion.op not in DISPLAY_ONLY_OPS
        and suggestion.status.value in _RUNNABLE_STATUSES
        and not rerun_blocked(suggestion.op, suggestion.status.value)
    )


def _summary_row(state: dict[str, Any]) -> None:
    """The card header's title and the filtered suggestion/entity summary.

    The old strip of eight counter cards is gone: the target design keeps one
    reading ("456 suggestions · 228 entities") beside the title, and the
    per-status detail is reachable through the Status filter. The figure is
    repainted by :func:`_render_summary` after every table load.
    """
    with ui.row().classes("items-baseline gap-3 no-wrap"):
        ui.label("Review").classes("uw-card-title")
        state["_summary_label"] = ui.label("").classes("uw-summary")
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

    "Actionable" is wider than "runnable": an APPLIED part of an in-place
    operation has no Run left, but it is exactly what the top Revert restores,
    so it counts toward the parent's tick too.
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
        actionable_subs = [
            sub for sub in subs if isinstance(sub, dict) and (sub.get("runnable") or sub.get("revert_selectable"))
        ]
        if actionable_subs and all(sub.get("id") in checked for sub in actionable_subs):
            selected.append(row)
    table.selected = selected
    table.update()
    _refresh_selection_counts(state)


def _toggle_expand_all(state: dict[str, Any]) -> None:
    """Expand every operation — or collapse them all when they all are expanded.

    The choice is stored as the page's expansion MODE, so the pages the
    operator flips to next open in the same state instead of collapsing.
    """
    table = state.get("_table")
    if table is None or table.is_deleted:
        return
    rows = table.rows or []
    keys = [str(row["row_key"]) for row in rows if isinstance(row, dict)]
    if not keys:
        return
    expanded = set(state.get("_expanded") or set())
    if all(key in expanded for key in keys):
        expanded = set()
        state["_expand_mode"] = "none"
    else:
        expanded = set(keys)
        state["_expand_mode"] = "all"
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
    # limit/offset, so this covers every operation the filter matches. Discarded
    # parts stay out (Restore is their undo), but APPLIED rows are selected too:
    # they cannot be written by a Run, yet they are exactly what the top Revert
    # restores. Each button counts its own side of the selection (see
    # ``_refresh_selection_counts``), so the extra rows never inflate Run (N).
    runnable = get_selection_rows(
        page_instance_url(),
        state["run_id"],
        _current_filter(state),
        runnable_only=True,
    )
    with_applied = get_selection_rows(
        page_instance_url(),
        state["run_id"],
        _current_filter(state),
        runnable_only=True,
        include_applied=True,
    )
    runnable_ids = {ref[0] for ref in runnable}
    state["_global_refs"] = {ref[0]: ref for ref in with_applied}
    state["_global_applied_ids"] = {ref[0] for ref in with_applied if ref[0] not in runnable_ids}
    state["_checked"] = set(state["_global_refs"])
    state["_select_all"] = True
    _sync_selection(state)


def _toggle_row(state: dict[str, Any], args: Any) -> None:
    """Apply one OPERATION checkbox (row_key, checked): the cascade's own entry.

    The row checkbox no longer writes Quasar's selection directly, so this is
    the only path a row tick takes: it propagates to the operation's parts and
    then re-derives the whole selection, which is what keeps the parent box and
    the part boxes from disagreeing (a row used to be able to show a tick while
    its parts were unchecked, or snap back on the next render).
    """
    if not isinstance(args, list) or len(args) != 2:
        return
    row_key, checked = args
    row = _row_of_key(state, row_key)
    if row is None:
        return
    _set_children_checked(state, row, bool(checked))
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


async def _update_table_now(state: dict[str, Any], client: Any) -> None:
    """Sync the table in place with Uwazi, then repaint the label, drift badge and grid."""
    credentials = credentials_for_instance(state["instance_url"])
    if credentials is None:
        with client:
            ui.notify("Not logged in to Uwazi", type="negative")
        return
    item = state.get("_update_table_item")
    # Syncing only READS Uwazi, so it takes no write gate: another run's write
    # pass must neither block it nor be blocked by it. It still takes this
    # run's claim, so the same run cannot be synced and Run at once.
    if not _claim_run_only(state, "refresh", client):
        return
    try:
        with client:
            if item is not None and not item.is_deleted:
                item.disable()
            # The BANNER reports the sync; the small "last synced" line keeps
            # its previous value instead of flashing a second syncing message.
            _apply_drift_state(state, None, syncing=True)
        result = await update_table_from_uwazi(
            state["instance_url"], state["run_id"], credentials.user, credentials.password
        )
        updated = get_drift_state(state["instance_url"], state["run_id"])
        if getattr(client, "is_deleted", False):
            # The page was closed while the sync ran. The sync itself landed;
            # do not touch a deleted client (NiceGUI warns and the UI is gone).
            return
        with client:
            _apply_drift_state(state, updated)
            label = state.get("_table_update_label")
            if label is not None and not label.is_deleted:
                label.text = table_update_label(get_table_update_time(state["instance_url"], state["run_id"]))
            ui.notify(result.message(), type="positive", multi_line=True)
            _refresh_all(state)
    except Exception as exc:  # noqa: BLE001 — the page reports, never crashes
        logger.exception("sync with Uwazi failed")
        if getattr(client, "is_deleted", False):
            return
        with client:
            _apply_drift_state(state, get_drift_state(state["instance_url"], state["run_id"]))
            ui.notify(
                f"Sync with Uwazi failed: {exc}",
                type="negative",
                multi_line=True,
            )
    finally:
        release_run(state["instance_url"], state["run_id"], "refresh")
        if item is not None and not item.is_deleted and not getattr(client, "is_deleted", False):
            with client:
                item.enable()
