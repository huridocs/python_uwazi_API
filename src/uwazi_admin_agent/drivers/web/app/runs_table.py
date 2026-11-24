"""Web driver module split out of the original ``app`` (behavior preserved)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from nicegui import app, context, ui

from uwazi_admin_agent.domain.run_name import free_copy_name
from uwazi_admin_agent.drivers.web import operation_gate

# Import for its side effect: the ``@ui.page("/review/{run_id}")`` route registers
# on import, so the runs table's Review action can navigate to it.
from uwazi_admin_agent.drivers.web import review_page as _review_page_module  # noqa: F401
from uwazi_admin_agent.drivers.web import task_page as _task_page_module  # noqa: F401
from uwazi_admin_agent.drivers.web.instances import page_link
from uwazi_admin_agent.drivers.web.prompt_chat_dialog import prompt_chat_dialog
from uwazi_admin_agent.drivers.web.run_service import (
    delete_run,
    export_tasks_csv,
    get_run,
    list_runs,
)
from uwazi_admin_agent.drivers.web.session import (
    credentials_for_instance,
    page_instance_url,
)

from .broadcast import (
    _run_rows,
    _sync_bulk_delete_button,
)
from .dialogs import _confirm_dialog, _delete_dialog, _error_dialog, _rename_dialog
from .state import (
    _ABORT,
    _IN_FLIGHT_JS,
    _STATUS_COLOR_JS,
    _busy_run_text,
    _release_bulk_claim,
    _run_is_busy,
    _session_ended,
    _try_claim_bulk,
)
from .wizard import _new_task_wizard, _start_generation


def _columns() -> list[dict[str, Any]]:
    return [
        {
            "name": "name",
            "label": "Task",
            "field": "name",
            "align": "left",
            "sortable": True,
            "style": "max-width: 260px; white-space: normal; word-break: break-word",
        },
        {
            "name": "status",
            "label": "Status",
            "field": "status",
            "align": "left",
            "sortable": True,
            "style": "min-width: 190px",
        },
        {"name": "created", "label": "Created", "field": "created", "align": "left", "sortable": True},
        {"name": "last_executed", "label": "Last execution", "field": "last_executed", "align": "left", "sortable": True},
        {"name": "modified", "label": "Modified", "field": "modified", "align": "right", "sortable": True},
        {"name": "deleted", "label": "Deleted", "field": "deleted", "align": "right", "sortable": True},
        {"name": "created_count", "label": "Created", "field": "created_count", "align": "right", "sortable": True},
        {"name": "rewired", "label": "Rewired", "field": "rewired", "align": "right", "sortable": True},
        {"name": "actions", "label": "Actions", "field": "actions", "align": "center", "sortable": False},
    ]


def _build_runs_table() -> ui.table:
    """Create the runs table once per client; refreshes update rows in place.

    The 5s auto-refresh used to delete and rebuild the whole table via
    ``@ui.refreshable``, which destroyed any open row menu. Reassigning
    ``table.rows`` and calling ``update()`` re-renders the cells reactively
    while an open ``q-menu`` (rendered in a portal) survives untouched.
    """
    table = (
        ui.table(rows=_run_rows(), columns=_columns(), row_key="id", pagination={"rowsPerPage": 0})
        .props("selection=multiple")
        .classes("w-full")
    )
    # The checkbox selection: the run ids the toolbar's Delete will remove.
    # Kept per client and pruned on refresh, so the 5s row push never leaves
    # stale ids in the selection (the same rule review_page applies).
    context.client._task_selection = []

    table.add_slot(
        "body-cell-status",
        f"""
        <q-td :props="props">
            <div class="column items-start">
                <q-badge :color="({_STATUS_COLOR_JS})[props.row.status] || 'grey'"
                         class="q-px1 q-py-2xs items-center">
                    <q-spinner v-if="{_IN_FLIGHT_JS}" size="12px" color="white" class="q-mr-xs" />
                    <span class="text-capitalize">{{{{ props.row.status }}}}</span>
                </q-badge>
                <div v-if="props.row.stage" class="text-caption text-grey-7 q-mt-xs"
                     style="max-width: 260px; white-space: normal;">{{{{ props.row.stage }}}}</div>
            </div>
        </q-td>
        """,
    )

    # Actions: execute, revert, info, then the row menu button LAST. The menu
    # itself is a page-level ``ui.menu`` (``_build_row_menu``): a menu nested in
    # the cell is unmounted whenever the 5s rows update re-renders the table
    # body — exactly the bug this design avoids. The ``rowmenu`` event carries
    # the row and the click event so the server can replay it on an off-screen
    # anchor at the cursor position.
    table.add_slot(
        "body-cell-actions",
        f"""
        <q-td :props="props" class="text-no-wrap">
            <q-btn dense flat icon="fact_check" color="secondary" title="Review &amp; apply"
                   @click="$parent.$emit('review', props.row)" />
            <q-btn dense flat icon="description" color="grey-8" title="Show prompt"
                   @click="$parent.$emit('info', props.row)" />
            <q-btn dense flat icon="more_vert" color="grey-8"
                   :disable="props.row.busy || {_IN_FLIGHT_JS}"
                   @click="$parent.$emit('rowmenu', props.row, $event)" />
        </q-td>
        """,
    )
    table.add_slot("no-data", '<div class="text-body1 text-grey-7 q-pa-md">No tasks</div>')

    table.on_select(_on_select)
    table.on("review", _on_review)
    table.on("info", _on_info)
    table.on("rowmenu", _on_rowmenu)
    table.on("errors", _on_errors)
    table.on("delete", _on_delete)
    return table


def _build_row_menu() -> None:
    """Build the page-level per-row menu (once per client, outside the table).

    Items are server-side elements, so opening the menu never re-renders the
    table; the selected run's id travels through ``app.storage.client``. The
    menu is shown by replaying the row click on an off-screen anchor with the
    recorded coordinates, so Quasar positions it at the cursor
    (``touch-position``). The conditional items (Retry / Error details) are
    toggled per selection in ``_on_rowmenu``.
    """
    with ui.button(icon="more_vert").props("flat dense").classes("fixed top-[-100px] left-[-100px]") as anchor:
        # Raw ``q-menu`` element: ``ui.menu`` refuses the ``touch-position`` prop,
        # which is what makes Quasar place the menu at the replayed click's
        # coordinates. ``auto-close`` covers what ``ui.menu_item``'s registered
        # close callback would do (the raw element isn't a ``ui.menu``).
        menu = ui.element("q-menu").props("touch-position auto-close")
        with menu:
            ui.menu_item("Review", lambda: _row_menu_action(_rowmenu_review))
            retry_item = ui.menu_item("Retry generation", lambda: _row_menu_action(_rowmenu_retry))
            errors_item = ui.menu_item("Error details", lambda: _row_menu_action(_error_dialog))
            ui.separator()
            ui.menu_item("Rename", lambda: _row_menu_action(_rename_dialog))
            ui.menu_item("Duplicate", lambda: _row_menu_action(_rowmenu_duplicate))
            ui.menu_item("Delete", lambda: _row_menu_action(_delete_dialog)).classes("text-negative")
    retry_item.set_visibility(False)
    errors_item.set_visibility(False)
    context.client._row_menu = menu  # noqa: SLF001 — per-client handle
    context.client._row_menu_anchor = anchor  # noqa: SLF001
    context.client._row_menu_retry = retry_item  # noqa: SLF001
    context.client._row_menu_errors = errors_item  # noqa: SLF001


def _row_menu_action(action: Any) -> None:
    """Run a menu action against the run selected in the row menu."""
    run_id = app.storage.client.get("rowmenu_run", "")
    if run_id:
        action(run_id)


def _rowmenu_review(run_id: str) -> None:
    """Jump to the run's review page, keeping the instance in the URL."""
    ui.navigate.to(page_link(f"/review/{run_id}"))


def _rowmenu_retry(run_id: str) -> None:
    """Delete the failed run and restart its generation with the same prompt."""
    instance = page_instance_url()
    if _session_ended(instance):
        return
    # Claim the run BEFORE the destructive delete: another operation on the
    # SAME run aborts the retry with no folder removed. Generation itself does
    # not take the Uwazi write gate: it does not change Uwazi.
    if not operation_gate.try_claim_run(instance, run_id, "creating"):
        ui.notify(_busy_run_text(instance, run_id), type="warning")
        return
    try:
        detail = get_run(instance, run_id)
    except Exception as exc:  # noqa: BLE001
        ui.notify(f"Failed to load run: {exc}", type="negative", multi_line=True)
        operation_gate.release_run(instance, run_id, "creating")
        return
    try:
        delete_run(instance, run_id)
    except Exception:
        # A failed delete must not leave the run claimed until restart.
        operation_gate.release_run(instance, run_id, "creating")
        raise
    credentials = credentials_for_instance(instance)
    if credentials is None:
        operation_gate.release_run(instance, run_id, "creating")
        ui.notify("Not logged in to Uwazi — log in again", type="negative")
        return
    _start_generation(
        run_id,
        detail.prompt,
        credentials.user,
        credentials.password,
        instance=instance,
        validated_prompt=detail.validated_prompt,
        claimed=True,
    )


def _rowmenu_duplicate(run_id: str) -> None:
    """Open the new-task wizard prefilled with this run's prompt + a free name.

    No busy claim: duplicating only opens a dialog; the claim is taken by
    ``_start_generation`` when the operator presses Generate.

    The prefill carries the run's FULL prompt — ``validated_prompt`` when it has
    one, i.e. the original request plus the validation step's resolved
    clarifications (and, after a chat Run, the whole conversation). Passing only
    ``detail.prompt`` dropped those clarifications, so a second generation
    planned a weaker request and could be refused with an empty table even
    though the first run worked.

    The name is numbered against the existing runs (``"<id> copy"``,
    ``"<id> copy 2"``, …) instead of being hardcoded: the prefilled wizard jumps
    straight to the Generate step, which skips ``_wizard_name_next``'s
    existing-name check — so a repeated duplicate silently regenerated the SAME
    run folder instead of creating a second task.
    """
    instance = page_instance_url()
    try:
        detail = get_run(instance, run_id)
        # In-flight generations have no manifest yet, so ``list_runs`` cannot see
        # them; their claims are exactly what a second duplicate would collide
        # with (and the claim is what ``_start_generation`` would refuse).
        taken = [run.run_id for run in list_runs(instance)]
        taken.extend(claim.run_id for claim in operation_gate.run_claims(instance))
    except Exception as exc:  # noqa: BLE001
        ui.notify(f"Failed to load run: {exc}", type="negative", multi_line=True)
        return
    _new_task_wizard(
        prefill_name=free_copy_name(run_id, taken),
        prefill_prompt=detail.prompt,
        prefill_validated_prompt=detail.validated_prompt,
    )


def _on_errors(e: Any) -> None:
    run_id = e.args["name"] if isinstance(e.args, dict) else e.args
    _error_dialog(run_id)


def _on_info(e: Any) -> None:
    run_id = e.args["name"] if isinstance(e.args, dict) else e.args
    prompt_chat_dialog(run_id)


def _on_select(e: Any) -> None:
    """Record the checkbox selection, pruned to the rows still visible.

    Reassigning ``table.rows`` on the 5s refresh re-renders the body without
    clearing the checkbox state, so stale ids must be dropped here (the same
    pruning rule review_page applies).
    """
    # ``on_select`` delivers a ``TableSelectionEventArguments`` (``.selection``);
    # keep the raw-list branch for direct test drivers.
    args = getattr(e, "args", None)
    if isinstance(args, list):
        selection = args
    else:
        selection = getattr(e, "selection", None) or []
    visible = {row["id"] for row in _run_rows()}
    context.client._task_selection = [row["id"] for row in selection if isinstance(row, dict) and row.get("id") in visible]
    # Enable/disable NOW, not on the next 5s refresh: the button is
    # Quasar-disabled until this runs, and a disabled q-btn silently eats clicks.
    _sync_bulk_delete_button()


def _selection_toolbar() -> None:
    """The row above the tasks table: New Task (solid primary) + bulk Delete.

    "New Task" is never disabled: creating a script does not change Uwazi, so
    an in-flight write elsewhere must not stop an operator from generating.
    """
    with ui.row().classes("w-full items-center q-mb-sm gap-2"):
        # Solid primary matches the review UI's Run button (`color="primary"`).
        ui.button("New Task", icon="add", on_click=_new_task_wizard).props("unelevated color=primary text-color=white")
        delete_button = ui.button("Delete", icon="delete", on_click=_bulk_delete_selected).props("dense flat color=negative")
        # Export is a READ: it does not need a selection and never blocks on an
        # in-flight run (the manifests it reads are already on disk).
        ui.button("Export CSV", icon="download", on_click=_export_tasks_csv).props("dense flat color=secondary").tooltip(
            "Download every task with its prompt and validated prompt as a CSV"
        )
        context.client._delete_selected_button = delete_button  # noqa: SLF001 — per-client enable state


def _export_tasks_csv() -> None:
    """Download every task of this instance as CSV, prompts included.

    The prompts are the point: the table shows counters only, and the operator
    needs the wording outside the app (review, diff, hand-off). A failure is
    reported as a toast — the file is generated in memory, so nothing partial
    reaches the browser.
    """
    instance = page_instance_url()
    if _session_ended(instance):
        return
    try:
        content = export_tasks_csv(instance)
    except Exception as exc:  # noqa: BLE001 — surface the failure, never a broken download
        ui.notify(f"Export failed: {exc}", type="negative", multi_line=True)
        return
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    ui.download.content(content.encode("utf-8"), filename=f"tasks-{stamp}.csv", media_type="text/csv")


def _bulk_delete_selected() -> None:
    """Open one confirmation for every checked task, then delete them all.

    The claims are taken when the operator CONFIRMS (not here), so closing the
    dialog leaves nothing held. Only a selected run that is ALREADY busy
    refuses the action; unrelated runs never block each other.
    """
    selected: list[str] = list(getattr(context.client, "_task_selection", []))
    if not selected:
        ui.notify("Nothing selected", type="warning")
        return
    instance = page_instance_url()
    if _session_ended(instance):
        return
    busy = [run_id for run_id in selected if _run_is_busy(instance, run_id)]
    if busy:
        ui.notify(
            "Cannot delete now: " + "; ".join(_busy_run_text(instance, run_id) for run_id in busy),
            type="warning",
            multi_line=True,
        )
        return

    names = ", ".join(repr(r) for r in selected)
    _confirm_dialog(
        "Delete selected tasks",
        f"Permanently remove {len(selected)} task(s): {names}? This deletes each run folder. "
        "If a run is EXECUTED, revert will no longer be possible.",
        lambda: _delete_selected_and_release(selected, instance),
        success_msg=f"Deleted {len(selected)} task(s)",
        instance=instance,
    )


def _delete_selected_and_release(selected: list[str], instance: str) -> Any:
    """Delete every selected run under ATOMIC per-run claims; always release."""
    if not _try_claim_bulk(instance, selected, "deleting"):
        busy = [run_id for run_id in selected if _run_is_busy(instance, run_id)]
        ui.notify(
            "Cannot delete now: " + "; ".join(_busy_run_text(instance, run_id) for run_id in busy),
            type="warning",
            multi_line=True,
        )
        return _ABORT
    deleted = 0
    try:
        for run_id in selected:
            delete_run(instance, run_id)
            deleted += 1
    finally:
        if deleted:
            selection = getattr(context.client, "_task_selection", [])
            context.client._task_selection = [run_id for run_id in selection if run_id not in set(selected)]
            # Quasar's footer banner counts ``table.selected``, not our list:
            # pruning only the ids left it saying "2 records selected" under a
            # table that no longer had those rows. The rows here are still the
            # STALE page payload, so ``_prune_table_selection`` cannot see the
            # deletion — drop the removed ids explicitly.
            table = getattr(context.client, "_runs_table", None)
            if table is not None and not table.is_deleted:
                removed = set(selected)
                table.selected = [
                    row for row in (table.selected or []) if not (isinstance(row, dict) and row.get("id") in removed)
                ]
                table.update()
        # Release AFTER pruning: its broadcast refreshes the toolbar from the
        # pruned selection, so the Delete button disables immediately.
        _release_bulk_claim(instance, selected)
    return None


def _on_review(e: Any) -> None:
    """Open the run's review page — the primary write path (accept writes there)."""
    run_id = e.args["name"] if isinstance(e.args, dict) else e.args
    ui.navigate.to(page_link(f"/review/{run_id}"))


def _on_delete(e: Any) -> None:
    run_id = e.args["name"] if isinstance(e.args, dict) else e.args
    _delete_dialog(run_id)


def _on_rowmenu(e: Any) -> None:
    """Open the page-level row menu for the clicked run at the click position.

    ``e.args`` is ``[row, click_event]``; only ``clientX``/``clientY`` survive
    event serialization. Conditional items (Retry / Error details) are toggled
    per the run's state, then the row's click is replayed on the off-screen
    anchor so Quasar positions the menu at the cursor (``touch-position``).
    """
    args = e.args if isinstance(e.args, list) else [e.args]
    row = args[0] if args else {}
    run_id = row["name"] if isinstance(row, dict) else row
    click = args[1] if len(args) > 1 and isinstance(args[1], dict) else {}
    app.storage.client["rowmenu_run"] = run_id
    show_retry = False
    show_errors = False
    try:
        detail = get_run(page_instance_url(), run_id)
        show_retry = detail.status.value == "generation_failed"
        show_errors = bool(detail.error)
    except Exception:  # noqa: BLE001 — a missing run just hides the conditional items
        pass
    context.client._row_menu_retry.set_visibility(show_retry)  # noqa: SLF001
    context.client._row_menu_errors.set_visibility(show_errors)  # noqa: SLF001

    anchor = context.client._row_menu_anchor  # noqa: SLF001
    client_x = int(click.get("clientX", 0) or 0)
    client_y = int(click.get("clientY", 0) or 0)
    ui.run_javascript(
        f"""
        (() => {{
          const anchor = getHtmlElement({anchor.id});
          anchor.dispatchEvent(new MouseEvent('click', {{clientX: {client_x}, clientY: {client_y}, bubbles: true}}));
        }})()
        """
    )
