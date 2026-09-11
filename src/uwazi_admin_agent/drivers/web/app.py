"""NiceGUI web UI for the admin agent — a driver replacing the CLI for common ops.

Single page: a table of runs with status badges + row actions (execute, revert,
info, and a per-row "more" menu with rename / history / error details / retry /
delete), and a "New Task" wizard (stepper) to create + generate a run. Mutating
operations run as background tasks; the table auto-refreshes every 5s by pushing
rows in place, so open row menus survive the refresh.

This is a driver: it wires the service layer (:mod:`run_service`) to the UI and
contains no business logic, matching the ``drivers/`` layer convention.
"""

import asyncio
import json
import os
import threading
from collections import deque
from typing import Any

from loguru import logger
from nicegui import app, background_tasks, context, ui

from uwazi_admin_agent.domain.execute_gate import ExecuteRefusedError
from uwazi_admin_agent.domain.prompt_validation import QuestionAnswer, build_final_prompt
from uwazi_admin_agent.domain.revert_gate import RevertRefusedError
from uwazi_admin_agent.drivers.web.run_service import (
    GenerateError,
    RevertVerificationError,
    RunSummary,
    clarify_prompt,
    clear_cache,
    create_and_generate,
    delete_run,
    execute_run,
    get_execution_history,
    get_run,
    get_run_audit,
    get_run_results,
    list_runs,
    rename_run,
    revert_run,
)
from uwazi_agent.adapters.uwazi_api.uwazi_api_adapter import UwaziApiAdapter

# Quasar color names for each status value (used by the status badge slot).
# "creating" is a UI-only status (not a persisted RunStatus) for runs whose
# generation is in flight — the manifest is only saved after generation completes.
_STATUS_COLORS: dict[str, str] = {
    "creating": "blue-grey",
    "planned": "grey",
    "snapshotted": "orange",
    "running": "blue",
    "reverting": "purple",
    "executed": "green",
    "verified": "blue",
    "reverted": "indigo",
    "failed": "red",
    "generation_failed": "deep-orange",
}

# Transient UI state: runs whose script is being generated (not yet persisted).
# The manifest is only saved after generation completes, so "creating" is a
# UI-only status tracked here — not a persisted RunStatus value.
_creating_runs: dict[str, dict[str, Any]] = {}

# Transient UI state: the "Generating script…" notification shown while a run's
# script is being generated. Keyed by run name so the background task can
# dismiss it once generation completes.
_generating_notifications: dict[str, Any] = {}

# Transient UI state: runs with an in-flight execute/rollback. The manifest
# still carries the pre-operation status until the background task finishes,
# so "running"/"reverting" are UI-only labels tracked here.
_running_runs: dict[str, str] = {}

# Global claim state: at most ONE mutating op (execute/revert/generate/rename/
# delete) may be in flight across ALL connected users. ``_active_op`` holds
# ``{"run_id": ..., "kind": ..., "label": ...}`` or None; the claim is the
# server-side serializer the JS-only guards cannot provide.
_active_op: dict[str, str] | None = None
_active_op_lock = threading.Lock()

_KIND_LABELS = {
    "running": "is running",
    "reverting": "is reverting",
    "creating": "is generating",
    "renaming": "is being renamed",
    "deleting": "is being deleted",
}


def _busy_label() -> str:
    """Human-readable busy banner text naming the active op, or '' when idle."""
    op = _active_op
    if op is None:
        return ""
    return f"Task {op['run_id']!r} {_KIND_LABELS.get(op['kind'], op['kind'])}"


def _is_busy() -> bool:
    return _active_op is not None


def _try_claim(run_id: str, kind: str) -> bool:
    """Atomically claim the single in-flight mutating op slot.

    Returns False when another op already holds the claim; on success the
    table overlay is registered and every client is refreshed so their
    controls disable immediately.
    """
    global _active_op
    with _active_op_lock:
        if _active_op is not None:
            return False
        _active_op = {"run_id": run_id, "kind": kind}
        # Rename/delete don't change a run's status; only run-level ops get
        # the transient status overlay (spinner badge) on their row.
        if kind in ("running", "reverting", "creating"):
            _running_runs[run_id] = kind
    _broadcast_rows()
    return True


def _release_run(run_id: str) -> None:
    """Release the claim when the finishing op owns it; refresh all clients."""
    global _active_op
    with _active_op_lock:
        if _active_op is None or _active_op["run_id"] != run_id:
            return
        _active_op = None
        _running_runs.pop(run_id, None)
    _broadcast_rows()


_IN_FLIGHT_JS = "['creating', 'running', 'reverting'].includes(props.row.status)"


def _can_execute_js(status_var: str) -> str:
    """JS expression: True when the run may be executed (no script on generation failure)."""
    return f"({status_var} !== 'generation_failed')"


def _mark_running(run_id: str, label: str) -> None:
    """Show a run's status as in-flight (spinner + label) in the table."""
    _running_runs[run_id] = label
    _broadcast_rows()


def _unmark_running(run_id: str) -> None:
    """Clear a run's in-flight status; the persisted status takes over."""
    _running_runs.pop(run_id, None)
    _broadcast_rows()


# JS set-literal string injected into the status badge slot for color lookup.
_STATUS_COLOR_JS = "{" + ", ".join(f"'{k}': '{v}'" for k, v in _STATUS_COLORS.items()) + "}"

# In-memory ring buffer of recent log lines (mirrors container stderr output).
# A loguru sink appends formatted lines here so the UI can display them live.
_LOG_BUFFER: deque[str] = deque(maxlen=2000)
# The Uwazi instance this admin agent controls (mirrored into the container via
# the UWAZI_URL env var). Shown in the header so the operator always knows which
# instance a generated run will mutate.
_CONTROLLED_UWAZI_URL = os.environ.get("UWAZI_URL", "not configured")


def _is_logged_in() -> bool:
    """True when the session holds cached Uwazi credentials."""
    return bool(app.storage.user.get("user") and app.storage.user.get("password"))


def _login_page() -> None:
    """Full-page login gate: validates the Uwazi account before the app loads.

    Nothing of the admin app renders until the credentials pass a real
    ``/api/login`` against the controlled Uwazi instance. Successful logins are
    cached in the session (``app.storage.user``) and the user is sent to
    ``/app``; failures show an inline error and keep the login page up.
    """
    ui.colors(primary="#2c3e50", secondary="#18bc9c", accent="#f39c12")
    with ui.column().classes("w-full items-center justify-center min-h-screen"):
        with ui.card().classes("w-full max-w-sm"):
            ui.label("Uwazi Admin Agent").classes("text-h6")
            ui.label(
                "Log in with the Uwazi account used to administer the instance. "
                "The page stays locked until the credentials are validated."
            ).classes("text-body1 q-mt-sm")
            user_input = ui.input("Username").classes("w-full text-h6")
            password_input = ui.input("Password", password=True).classes("w-full text-h6")
            error_label = ui.label().classes("text-negative text-body2")

            def _submit() -> None:
                # Empty fields default to the local admin account (admin/admin).
                user = (user_input.value or "").strip() or "admin"
                password = password_input.value or "admin"
                try:
                    UwaziApiAdapter(user=user, password=password, url=_CONTROLLED_UWAZI_URL)
                except Exception as exc:  # noqa: BLE001 — surface every validation failure
                    error_label.set_text(f"Login failed: {exc}")
                    return
                app.storage.user["user"] = user
                app.storage.user["password"] = password
                ui.navigate.to("/app")

            password_input.on("keydown.enter", _submit)
            with ui.row().classes("q-mt-lg"):
                ui.button("Log in", color="primary", on_click=_submit)


def _logout() -> None:
    """Drop the session credentials and return to the login page."""
    app.storage.user.pop("user", None)
    app.storage.user.pop("password", None)
    ui.notify("Logged out", type="positive")
    ui.navigate.to("/")


def _log_sink(message: Any) -> None:
    """Loguru sink: append the formatted log record to the ring buffer."""
    _LOG_BUFFER.append(str(message).rstrip("\n"))


logger.add(_log_sink, level="DEBUG", format="{time:HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}")


def _broadcast_notify(message: str, type: str = "positive", **kwargs: Any) -> None:
    """Send a notification to every connected client (safe from background tasks).

    ``ui.notify`` requires a client context, which background tasks lack. This
    iterates all connected clients and enqueues a notify message to each.
    """
    options = {"message": str(message), "type": type, **kwargs}
    for client in app.clients():
        client.outbox.enqueue_message("notify", options, client.id)


def _notify_error(title: str, detail: str, type_: str = "negative") -> None:
    """Short headline toast; the full detail lives in the run's error dialog."""
    first_line = (detail or "").strip().splitlines()[0] if (detail or "").strip() else title
    _broadcast_notify(title, type=type_, caption=first_line)


def _summary_to_row(run: RunSummary) -> dict[str, Any]:
    return {
        "id": run.run_id,
        "name": run.run_id,
        "status": run.status.value,
        "created": run.created_at.strftime("%Y-%m-%d %H:%M"),
        "last_executed": run.last_executed_at.strftime("%Y-%m-%d %H:%M") if run.last_executed_at else "—",
        "modified": run.modified,
        "deleted": run.deleted,
        "created_count": run.created,
        "rewired": run.rewired,
        "error": bool(run.error),
        "busy": _is_busy(),
    }


def _creating_to_row(name: str) -> dict[str, Any]:
    """A placeholder row for a run whose generation is in flight."""
    return {
        "id": name,
        "name": name,
        "status": "creating",
        "created": "—",
        "last_executed": "—",
        "modified": 0,
        "deleted": 0,
        "created_count": 0,
        "rewired": 0,
        "error": False,
        "busy": True,
    }


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
        {"name": "status", "label": "Status", "field": "status", "align": "left", "sortable": True},
        {"name": "created", "label": "Created", "field": "created", "align": "left", "sortable": True},
        {"name": "last_executed", "label": "Last execution", "field": "last_executed", "align": "left", "sortable": True},
        {"name": "modified", "label": "Modified", "field": "modified", "align": "right", "sortable": True},
        {"name": "deleted", "label": "Deleted", "field": "deleted", "align": "right", "sortable": True},
        {"name": "created_count", "label": "Created", "field": "created_count", "align": "right", "sortable": True},
        {"name": "rewired", "label": "Rewired", "field": "rewired", "align": "right", "sortable": True},
        {"name": "actions", "label": "Actions", "field": "actions", "align": "center", "sortable": False},
    ]


def _can_revert_js(status_var: str) -> str:
    """JS expression: True when the run has changes that revert can undo."""
    return f"({status_var} === 'executed' || {status_var} === 'failed')"


def _run_rows() -> list[dict[str, Any]]:
    """Compute the current table rows (persisted runs + transient placeholders)."""
    runs = list_runs()
    persisted_ids = {r.run_id for r in runs}
    rows = [_summary_to_row(r) for r in runs]
    # Merge in runs whose generation is still in flight (not yet persisted).
    for name in _creating_runs:
        if name not in persisted_ids:
            rows.append(_creating_to_row(name))
    # Overlay in-flight execute/rollback ops onto persisted rows.
    for run_id, label in _running_runs.items():
        for row in rows:
            if row["id"] == run_id:
                row["status"] = label
    return rows


def _build_runs_table() -> ui.table:
    """Create the runs table once per client; refreshes update rows in place.

    The 5s auto-refresh used to delete and rebuild the whole table via
    ``@ui.refreshable``, which destroyed any open row menu. Reassigning
    ``table.rows`` and calling ``update()`` re-renders the cells reactively
    while an open ``q-menu`` (rendered in a portal) survives untouched.
    """
    table = ui.table(rows=_run_rows(), columns=_columns(), row_key="id", pagination={"rowsPerPage": 0})

    table.add_slot(
        "body-cell-status",
        f"""
        <q-td :props="props">
            <q-badge :color="({_STATUS_COLOR_JS})[props.row.status] || 'grey'"
                     class="q-px1 q-py-2xs items-center">
                <q-spinner v-if="{_IN_FLIGHT_JS}" size="12px" color="white" class="q-mr-xs" />
                <span class="text-capitalize">{{{{ props.row.status }}}}</span>
            </q-badge>
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
            <q-btn dense flat icon="play_arrow" color="primary"
                   :disable="props.row.busy || {_IN_FLIGHT_JS} || !{_can_execute_js("props.row.status")}"
                   @click="$parent.$emit('execute', props.row)" />
            <q-btn dense flat icon="undo" color="warning"
                   :disable="props.row.busy || {_IN_FLIGHT_JS} || !{_can_revert_js("props.row.status")}"
                   @click="$parent.$emit('rollback', props.row)" />
            <q-btn dense flat icon="info" color="grey-8"
                   @click="$parent.$emit('info', props.row)" />
            <q-btn dense flat icon="more_vert" color="grey-8"
                   :disable="props.row.busy || {_IN_FLIGHT_JS}"
                   @click="$parent.$emit('rowmenu', props.row, $event)" />
        </q-td>
        """,
    )
    table.add_slot("no-data", '<div class="text-body1 text-grey-7 q-pa-md">No tasks</div>')

    table.on("execute", _on_execute)
    table.on("rollback", _on_rollback)
    table.on("info", _on_info)
    table.on("rowmenu", _on_rowmenu)
    table.on("history", _on_history)
    table.on("errors", _on_errors)
    table.on("delete", _on_delete)
    return table


def _build_row_menu() -> None:
    """Build the page-level per-row menu (once per client, outside the table).

    Items are server-side elements, so opening the menu never re-renders the
    table; the selected run's id travels through ``app.storage.client``. The
    menu is shown by replaying the row click on an off-screen anchor with the
    recorded coordinates, so Quasar positions it at the cursor
    (``touch-position``). The four conditional items (Show script / Show results /
    Retry / Error details) are toggled per selection in ``_on_rowmenu``.
    """
    with ui.button(icon="more_vert").props("flat dense").classes("fixed top-[-100px] left-[-100px]") as anchor:
        # Raw ``q-menu`` element: ``ui.menu`` refuses the ``touch-position`` prop,
        # which is what makes Quasar place the menu at the replayed click's
        # coordinates. ``auto-close`` covers what ``ui.menu_item``'s registered
        # close callback would do (the raw element isn't a ``ui.menu``).
        menu = ui.element("q-menu").props("touch-position auto-close")
        with menu:
            retry_item = ui.menu_item("Retry generation", lambda: _row_menu_action(_rowmenu_retry))
            errors_item = ui.menu_item("Error details", lambda: _row_menu_action(_error_dialog))
            ui.separator()
            ui.menu_item("Rename", lambda: _row_menu_action(_rename_dialog))
            ui.menu_item("Duplicate", lambda: _row_menu_action(_rowmenu_duplicate))
            ui.menu_item("History", lambda: _row_menu_action(_history_dialog))
            script_item = ui.menu_item("Show script", lambda: _row_menu_action(_script_dialog))
            results_item = ui.menu_item("Show results", lambda: _row_menu_action(_results_dialog))
            ui.menu_item("Delete", lambda: _row_menu_action(_delete_dialog)).classes("text-negative")
    retry_item.set_visibility(False)
    errors_item.set_visibility(False)
    script_item.set_visibility(False)
    results_item.set_visibility(False)
    context.client._row_menu = menu  # noqa: SLF001 — per-client handle
    context.client._row_menu_anchor = anchor  # noqa: SLF001
    context.client._row_menu_retry = retry_item  # noqa: SLF001
    context.client._row_menu_errors = errors_item  # noqa: SLF001
    context.client._row_menu_script = script_item  # noqa: SLF001
    context.client._row_menu_results = results_item  # noqa: SLF001


def _row_menu_action(action: Any) -> None:
    """Run a menu action against the run selected in the row menu."""
    run_id = app.storage.client.get("rowmenu_run", "")
    if run_id:
        action(run_id)


def _rowmenu_retry(run_id: str) -> None:
    """Delete the failed run and restart its generation with the same prompt."""
    # Claim BEFORE the destructive delete: a busy claim aborts the retry with
    # no folder removed. The claim is released by _do_generate's finally
    # (or re-took here implicitly if creation fails — the finally covers it).
    if not _try_claim(run_id, "creating"):
        ui.notify(_busy_label(), type="warning")
        return
    try:
        detail = get_run(run_id)
    except Exception as exc:  # noqa: BLE001
        ui.notify(f"Failed to load run: {exc}", type="negative", multi_line=True)
        _release_run(run_id)
        return
    delete_run(run_id)
    _start_generation(
        run_id,
        detail.prompt,
        app.storage.user["user"],
        app.storage.user["password"],
        validated_prompt=detail.validated_prompt,
        claimed=True,
    )


def _rowmenu_duplicate(run_id: str) -> None:
    """Open the new-task wizard prefilled with this run's name + prompt.

    No busy claim: duplicating only opens a dialog; the claim is taken by
    ``_start_generation`` when the operator presses Generate. A name collision
    (``"<name> copy"`` already existing) is caught by the wizard's own
    duplicate-name validation when the operator goes back to edit.
    """
    try:
        detail = get_run(run_id)
    except Exception as exc:  # noqa: BLE001
        ui.notify(f"Failed to load run: {exc}", type="negative", multi_line=True)
        return
    _new_task_wizard(prefill_name=f"{run_id} copy", prefill_prompt=detail.prompt)


def _on_history(e: Any) -> None:
    run_id = e.args["name"] if isinstance(e.args, dict) else e.args
    _history_dialog(run_id)


def _on_errors(e: Any) -> None:
    run_id = e.args["name"] if isinstance(e.args, dict) else e.args
    _error_dialog(run_id)


def _on_info(e: Any) -> None:
    run_id = e.args["name"] if isinstance(e.args, dict) else e.args
    _info_dialog(run_id)


def _refresh_rows_client() -> None:
    """Push fresh rows + busy state into this client's live page."""
    table = getattr(context.client, "_runs_table", None)
    if table is None or table.is_deleted:
        return
    table.rows = _run_rows()
    table.update()
    # Busy banner: a per-client label above the card, visible only while some
    # mutating op is in flight (any user's op — the claim is global).
    banner = getattr(context.client, "_busy_banner", None)
    if banner is not None and not banner.is_deleted:
        label = _busy_label()
        banner.set_visibility(bool(label))
        if label:
            banner.text = label
    # The per-client New Task button mirrors the busy state.
    new_task = getattr(context.client, "_new_task_button", None)
    if new_task is not None and not new_task.is_deleted:
        new_task.set_enabled(not _is_busy())


def _broadcast_rows() -> None:
    """Refresh table rows on every connected client (safe from background tasks)."""
    for client in app.clients():
        with client:
            _refresh_rows_client()


def _on_execute(e: Any) -> None:
    run_id = e.args["name"] if isinstance(e.args, dict) else e.args
    if not _try_claim(run_id, "running"):
        ui.notify(_busy_label(), type="warning")
        return
    background_tasks.create(
        _run_async(
            execute_run(run_id, app.storage.user["user"], app.storage.user["password"]),
            f"Executed {run_id}",
            run_id,
        ),
        name=f"execute {run_id}",
    )


def _on_rollback(e: Any) -> None:
    run_id = e.args["name"] if isinstance(e.args, dict) else e.args
    _confirm_dialog(
        "Rollback run",
        f"Revert run {run_id!r}? This restores every backed-up entity and deletes created ones.",
        lambda: revert_run(run_id, app.storage.user["user"], app.storage.user["password"]),
        success_msg=f"Reverted {run_id}",
        run_id=run_id,
    )


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
    show_script = False
    show_results = False
    try:
        detail = get_run(run_id)
        show_retry = detail.status.value == "generation_failed"
        show_errors = bool(detail.error)
        show_script = bool(detail.script)
        show_results = detail.status.value in ("executed", "verified", "failed", "reverted")
    except Exception:  # noqa: BLE001 — a missing run just hides the conditional items
        pass
    context.client._row_menu_retry.set_visibility(show_retry)  # noqa: SLF001
    context.client._row_menu_errors.set_visibility(show_errors)  # noqa: SLF001
    context.client._row_menu_script.set_visibility(show_script)  # noqa: SLF001
    context.client._row_menu_results.set_visibility(show_results)  # noqa: SLF001

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


async def _run_async(coro: Any, success_msg: str, run_id: str | None = None) -> None:
    """Await a coroutine operation, notify on success/error, then refresh the table.

    The service coroutines (execute/rollback) perform *synchronous* HTTP via the
    ``requests``-based Uwazi client, which would block the event loop for the
    whole operation — stalling the outbox (so the in-flight badge and table
    refresh never reach the browser) and the websocket heartbeat (connection
    drops). Running the coroutine on a worker thread keeps the loop free.
    """
    try:
        await asyncio.to_thread(asyncio.run, coro)
        _broadcast_notify(success_msg, type="positive")
    except GenerateError as exc:
        _notify_error("Generation failed", str(exc))
    except RevertVerificationError as exc:
        _notify_error("Revert completed but verification found mismatches", str(exc))
    except (ExecuteRefusedError, RevertRefusedError) as exc:
        _notify_error("Refused", str(exc), type_="warning")
    except Exception as exc:  # noqa: BLE001 — surface every failure to the operator
        _notify_error("Error", str(exc))
    finally:
        if run_id is not None:
            _unmark_running(run_id)
            _release_run(run_id)
        _broadcast_rows()


def _confirm_dialog(
    title: str,
    message: str,
    on_confirm: Any,
    success_msg: str,
    run_id: str | None = None,
    kind: str = "reverting",
) -> None:
    """Yes/no confirmation dialog; runs ``on_confirm`` (sync or async) on confirm.

    Created on the page layout (not inside the refreshable table) so the 5s
    auto-refresh doesn't destroy it.
    """
    with context.client.layout:
        with ui.dialog() as dialog, ui.card():
            ui.label(title).classes("text-h6")
            ui.label(message).classes("text-body1")
            with ui.row().classes("w-full justify-end"):
                ui.button("Cancel", on_click=lambda: dialog.close())
                ui.button(
                    "Confirm",
                    color="negative",
                    on_click=lambda: _confirm_and_close(dialog, on_confirm, success_msg, run_id, kind),
                )
    dialog.open()


def _rename_dialog(run_id: str) -> None:
    """Prompt for a new task name and rename the run in a background task.

    Created on the page layout (not inside the refreshable table) so the 5s
    auto-refresh doesn't destroy it.
    """
    with context.client.layout:
        with ui.dialog() as dialog, ui.card().classes("w-full max-w-lg"):
            dialog.props("persistent")
            ui.label(f"Rename task — {run_id}").classes("text-h6")
            name_input = ui.input(
                "New name",
                value=run_id,
                validation={"Required": lambda v: bool(v and v.strip())},
            ).classes("w-full text-h6")
            with ui.row().classes("q-mt-lg w-full justify-end"):
                ui.button("Cancel", on_click=lambda: dialog.close()).props("color=grey-7 flat")
                ui.button(
                    "Rename",
                    icon="edit",
                    on_click=lambda: _rename_confirm(dialog, run_id, name_input),
                )
    dialog.open()


def _copy_to_clipboard(text: str, label: str = "Copy") -> None:
    """Button that copies ``text`` to the clipboard via the browser API.

    Uses ``navigator.clipboard.writeText`` when available (secure contexts);
    otherwise falls back to a hidden textarea + ``document.execCommand('copy')``.
    """
    payload = json.dumps(text)
    js = (
        f"navigator.clipboard ? navigator.clipboard.writeText({payload}) : (() => {{ "
        f"const el = document.createElement('textarea'); el.value = {payload}; "
        "el.style.position = 'fixed'; el.style.opacity = '0'; "
        "document.body.appendChild(el); el.select(); "
        "document.execCommand('copy'); el.remove(); })()"
    )

    def _do_copy() -> None:
        ui.run_javascript(js)
        ui.notify("Copied to clipboard", type="positive", timeout=1.5)

    ui.button(label, icon="content_copy", on_click=_do_copy).props("flat dense color=grey-7")


def _info_dialog(run_id: str) -> None:
    """Modal: the run's name and prompt (plus any recorded error hint).

    Created on the page layout (not inside the table) so the row auto-refresh
    can't destroy it. Read-only; actions live in the row's ``more_vert`` menu.
    """
    try:
        detail = get_run(run_id)
    except Exception as exc:  # noqa: BLE001
        ui.notify(f"Failed to load run: {exc}", type="negative", multi_line=True)
        return

    with context.client.layout:
        with ui.dialog() as dialog, ui.card().classes("w-full max-w-2xl max-h-[85vh]").style("overflow-y: auto"):
            ui.label(f"Task — {run_id}").classes("text-h6")
            ui.separator()
            with ui.row().classes("w-full items-center justify-between"):
                ui.label("Prompt").classes("text-subtitle1 text-grey-7")
                _copy_to_clipboard(detail.prompt or "", "Copy prompt")
            ui.textarea(value=detail.prompt or "").classes("w-full font-mono").props("readonly outlined autogrow").style(
                "min-height: 120px"
            )
            if detail.validated_prompt:
                ui.separator()
                with ui.row().classes("w-full items-center justify-between"):
                    ui.label("Validated prompt").classes("text-subtitle1 text-grey-7")
                    _copy_to_clipboard(detail.validated_prompt, "Copy validated prompt")
                ui.textarea(value=detail.validated_prompt).classes("w-full font-mono").props(
                    "readonly outlined autogrow"
                ).style("min-height: 120px")
            if detail.error:
                ui.separator()
                ui.label("Last error").classes("text-subtitle1 text-grey-7")
                ui.label(detail.error.strip().splitlines()[0]).classes("text-body2 text-red-10")
            with ui.row().classes("w-full q-mt-lg justify-end"):
                ui.button("Close", on_click=dialog.close).props("color=grey-7 flat")
    dialog.open()


def _history_dialog(run_id: str) -> None:
    """Modal: a run's execute/revert history (time, type, outcome).

    Created on the page layout (not inside the refreshable table) so the 5s
    auto-refresh doesn't destroy it. Reads the run's audit log via
    ``get_execution_history``; a run with no executions shows an empty state.
    """
    events = get_execution_history(run_id)
    with context.client.layout:
        with ui.dialog() as dialog, ui.card().classes("w-full max-w-2xl"):
            ui.label(f"Execution history — {run_id}").classes("text-h6")
            if not events:
                ui.label("No executions recorded yet.").classes("text-grey-7 q-pa-md")
            else:
                rows = [
                    {
                        "time": e.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                        "type": e.type.capitalize(),
                        "outcome": e.outcome,
                        "detail": e.detail or "",
                    }
                    for e in events
                ]
                columns = [
                    {"name": "time", "label": "Time (UTC)", "field": "time", "align": "left", "sortable": True},
                    {"name": "type", "label": "Type", "field": "type", "align": "left", "sortable": True},
                    {"name": "outcome", "label": "Outcome", "field": "outcome", "align": "left", "sortable": True},
                    {"name": "detail", "label": "Detail", "field": "detail", "align": "left", "sortable": False},
                ]
                ui.table(
                    rows=rows,
                    columns=columns,
                    row_key="time",
                    pagination={"rowsPerPage": 0},
                ).classes("w-full")
            with ui.row().classes("w-full q-mt-md justify-end"):
                ui.button("Close", on_click=dialog.close).props("color=grey-7 flat")
    dialog.open()


def _error_dialog(run_id: str) -> None:
    """Modal: the run's last error detail plus its full audit trail.

    Created on the page layout (not inside the refreshable table) so the 5s
    auto-refresh doesn't destroy it — same pattern as ``_logs_dialog``.

    The dialog is maximized (top to bottom): the error detail and audit trail
    share the space between a pinned title and a pinned footer, so the Close
    button is always visible without scrolling.
    """
    try:
        detail = get_run(run_id)
        records = get_run_audit(run_id)
    except Exception as exc:  # noqa: BLE001
        ui.notify(f"Failed to load run: {exc}", type="negative", multi_line=True)
        return

    with context.client.layout:
        with ui.dialog() as dialog, ui.card().classes("w-full").props("style=height:100dvh"):
            dialog.props("maximized")
            with ui.column().classes("w-full h-full items-stretch no-wrap"):
                with ui.row().classes("w-full items-center justify-between"):
                    ui.label(f"Error details — {run_id}").classes("text-h6")
                if detail.error:
                    ui.textarea(value=detail.error).classes("w-full grow font-mono text-caption").props(
                        "readonly outlined"
                    ).style("min-height: 0")
                else:
                    ui.label("No error recorded on this run.").classes("text-grey-7")
                with ui.column().classes("w-full shrink-0").style("max-height: 40%; overflow-y: auto"):
                    ui.label("Audit trail").classes("text-subtitle1 q-mt-lg")
                    if not records:
                        ui.label("No audit records.").classes("text-grey-7")
                    else:
                        rows = [
                            {
                                "time": r.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                                "step": r.step.value,
                                "op": r.op_kind,
                                "outcome": r.outcome.value,
                                "detail": r.detail or "",
                            }
                            for r in records
                        ]
                        columns = [
                            {"name": "time", "label": "Time (UTC)", "field": "time", "align": "left", "sortable": True},
                            {"name": "step", "label": "Step", "field": "step", "align": "left", "sortable": True},
                            {"name": "op", "label": "Op", "field": "op", "align": "left", "sortable": True},
                            {"name": "outcome", "label": "Outcome", "field": "outcome", "align": "left", "sortable": True},
                            {"name": "detail", "label": "Detail", "field": "detail", "align": "left", "sortable": False},
                        ]
                        ui.table(
                            rows=rows,
                            columns=columns,
                            row_key="time",
                            pagination={"rowsPerPage": 0},
                        ).classes("w-full")
                with ui.row().classes("w-full justify-end"):
                    ui.button("Close", icon="close", on_click=dialog.close).props("flat")
    dialog.open()


def _script_dialog(run_id: str) -> None:
    """Modal: the run's generated Python script (only reachable when a script exists).

    Created on the page layout (not inside the refreshable table) so the 5s
    auto-refresh doesn't destroy it — same pattern as ``_error_dialog``. The
    run's generated script lives at ``<run>/script.py``; a run whose generation
    failed has none, and the menu item is hidden in that case.

    The dialog is maximized and the card scrolls as a whole, so the prompt and
    script keep their natural (autogrow) sizes and long content simply extends
    the page instead of overlapping.
    """
    try:
        detail = get_run(run_id)
    except Exception as exc:  # noqa: BLE001
        ui.notify(f"Failed to load run: {exc}", type="negative", multi_line=True)
        return
    if not detail.script:
        ui.notify("No generated script on this run.", type="warning")
        return

    with context.client.layout:
        with ui.dialog() as dialog, ui.card().classes("w-full").props("style=height:100dvh; overflow-y: auto"):
            dialog.props("maximized")
            with ui.column().classes("w-full items-stretch"):
                with ui.row().classes("w-full items-center justify-between"):
                    ui.label(f"Generated script — {run_id}").classes("text-h6")
                    _copy_to_clipboard(
                        f"Prompt:\n{detail.prompt or ''}\n\n"
                        f"Validated prompt:\n{detail.validated_prompt or ''}\n\n"
                        f"Script:\n{detail.script}",
                        "Copy all",
                    )
                with ui.row().classes("w-full items-center justify-between"):
                    ui.label("Prompt").classes("text-subtitle1 text-grey-7")
                    _copy_to_clipboard(detail.prompt or "", "Copy prompt")
                ui.textarea(value=detail.prompt or "").classes("w-full font-mono").props("readonly outlined autogrow").style(
                    "min-height: 80px"
                )
                if detail.validated_prompt:
                    with ui.row().classes("w-full items-center justify-between"):
                        ui.label("Validated prompt").classes("text-subtitle1 text-grey-7")
                        _copy_to_clipboard(detail.validated_prompt, "Copy validated prompt")
                    ui.textarea(value=detail.validated_prompt).classes("w-full font-mono").props(
                        "readonly outlined autogrow"
                    ).style("min-height: 80px")
                with ui.row().classes("w-full items-center justify-between"):
                    ui.label("Script").classes("text-subtitle1 text-grey-7")
                    _copy_to_clipboard(detail.script, "Copy script")
                ui.code(detail.script, language="python").classes("w-full")
                with ui.row().classes("w-full justify-end"):
                    ui.button("Close", icon="close", on_click=dialog.close).props("flat")
    dialog.open()


def _format_value(value: Any) -> str:
    """Render an arbitrary manifest value (e.g. a rewired 'before' state) as text."""
    if value is None:
        return "—"
    text = str(value)
    return text if len(text) <= 200 else text[:200] + "…"


def _entity_section(title: str, entities: list[Any], include_restored: bool = False) -> None:
    """Render one touch-set section (modified/created/deleted) as a table, if non-empty."""
    if not entities:
        return
    ui.label(title).classes("text-subtitle1 q-mt-md")
    columns = [
        {"name": "shared_id", "label": "Shared ID", "field": "shared_id", "align": "left", "sortable": True},
        {"name": "internal_id", "label": "Internal ID", "field": "internal_id", "align": "left", "sortable": True},
        {"name": "language", "label": "Language", "field": "language", "align": "left", "sortable": True},
    ]
    if include_restored:
        columns.append(
            {
                "name": "restored_shared_id",
                "label": "Restored shared ID",
                "field": "restored_shared_id",
                "align": "left",
                "sortable": False,
            }
        )
    rows = []
    for e in entities:
        row = {
            "shared_id": e.shared_id,
            "internal_id": e.internal_id or "—",
            "language": e.language or "—",
        }
        if include_restored:
            row["restored_shared_id"] = e.restored_shared_id or "—"
        rows.append(row)
    ui.table(rows=rows, columns=columns, row_key="shared_id", pagination={"rowsPerPage": 0}).classes("w-full")


def _rewired_section(rewired: list[Any]) -> None:
    """Render the rewired-relationships touch-set as a table, if non-empty."""
    if not rewired:
        return
    ui.label("Rewired relationships").classes("text-subtitle1 q-mt-md")
    columns = [
        {"name": "entity", "label": "Entity", "field": "entity", "align": "left", "sortable": True},
        {"name": "property", "label": "Property", "field": "property", "align": "left", "sortable": True},
        {"name": "before", "label": "Before", "field": "before", "align": "left", "sortable": False},
    ]
    rows = [{"entity": r.entity.shared_id, "property": r.property_name, "before": _format_value(r.before)} for r in rewired]
    ui.table(rows=rows, columns=columns, pagination={"rowsPerPage": 0}).classes("w-full")


def _files_section(files: list[Any]) -> None:
    """Render the deleted-files touch-set as a table, if non-empty."""
    if not files:
        return
    ui.label("Deleted files").classes("text-subtitle1 q-mt-md")
    columns = [
        {"name": "shared_id", "label": "Entity", "field": "shared_id", "align": "left", "sortable": True},
        {"name": "originalname", "label": "File", "field": "originalname", "align": "left", "sortable": True},
        {"name": "kind", "label": "Kind", "field": "kind", "align": "left", "sortable": True},
        {"name": "source", "label": "Source", "field": "source", "align": "left", "sortable": True},
    ]
    rows = [{"shared_id": f.shared_id, "originalname": f.originalname, "kind": f.kind, "source": f.source} for f in files]
    ui.table(rows=rows, columns=columns, pagination={"rowsPerPage": 0}).classes("w-full")


def _results_dialog(run_id: str) -> None:
    """Modal: the run's execution results — its `result` value and touch-set.

    Created on the page layout (not inside the refreshable table) so the 5s
    auto-refresh doesn't destroy it — same pattern as ``_script_dialog``. Reads
    the manifest's touch-set via ``get_run_results``; a run with no touch-set
    (never executed) shows an empty state. The script's `result` value (the
    answer for a query task, or the summary for a mutation task) is shown first.

    The dialog is maximized (top to bottom): the result + summary + section
    tables fill the space between a pinned title and a pinned footer, scrolling
    internally so the Close button is always visible.
    """
    try:
        results = get_run_results(run_id)
    except Exception as exc:  # noqa: BLE001
        ui.notify(f"Failed to load run: {exc}", type="negative", multi_line=True)
        return

    with context.client.layout:
        with ui.dialog() as dialog, ui.card().classes("w-full").props("style=height:100dvh"):
            dialog.props("maximized")
            with ui.column().classes("w-full h-full items-stretch no-wrap"):
                with ui.row().classes("w-full items-center justify-between"):
                    ui.label(f"Run results — {run_id}").classes("text-h6")
                with ui.column().classes("w-full grow").style("overflow-y: auto"):
                    if results.result is not None:
                        ui.label("Result").classes("text-subtitle1 text-grey-7")
                        ui.textarea(value=results.result).classes("w-full font-mono").props(
                            "readonly outlined autogrow"
                        ).style("min-height: 60px")
                        ui.separator()
                    counts = [
                        ("Modified", len(results.modified)),
                        ("Created", len(results.created)),
                        ("Deleted", len(results.deleted)),
                        ("Rewired", len(results.rewired)),
                        ("Files deleted", len(results.deleted_files)),
                    ]
                    with ui.row().classes("w-full q-mb-md gap-md"):
                        for label, count in counts:
                            with ui.column().classes("items-center"):
                                ui.label(str(count)).classes("text-h5")
                                ui.label(label).classes("text-caption text-grey-7")
                    if not any(c for _, c in counts) and results.result is None:
                        ui.label("No results recorded on this run.").classes("text-grey-7 q-pa-md")
                    _entity_section("Modified entities", results.modified)
                    _entity_section("Created entities", results.created)
                    _entity_section("Deleted entities", results.deleted, include_restored=True)
                    _rewired_section(results.rewired)
                    _files_section(results.deleted_files)
                with ui.row().classes("w-full justify-end"):
                    ui.button("Close", icon="close", on_click=dialog.close).props("flat")
    dialog.open()


def _delete_dialog(run_id: str) -> None:
    """Yes/no confirmation before permanently removing a run."""
    _confirm_dialog(
        "Delete run",
        f"Permanently remove run {run_id!r}? This deletes the entire run folder. "
        "If the run is EXECUTED, revert will no longer be possible.",
        lambda: delete_run(run_id),
        success_msg=f"Deleted {run_id}",
        run_id=run_id,
        kind="deleting",
    )


def _rename_confirm(dialog: Any, old_id: str, name_input: Any) -> None:
    """Validate the new name, then rename the run via a background task."""
    new_id = (name_input.value or "").strip()
    if not new_id:
        ui.notify("New name is required", type="warning")
        return
    if new_id == old_id:
        dialog.close()
        return
    existing = {r.run_id for r in list_runs()}
    if new_id in existing:
        ui.notify(f"A task named {new_id!r} already exists", type="warning")
        return
    if not _try_claim(new_id, "renaming"):
        ui.notify(_busy_label(), type="warning")
        return
    dialog.close()

    async def _do() -> None:
        rename_run(old_id, new_id)

    background_tasks.create(_run_async(_do(), f"Renamed {old_id} → {new_id}", new_id), name=f"rename {old_id}")


def _logs_dialog() -> None:
    """Show recent service logs in a maximized, scrollable modal (like ``docker compose logs``).

    Created on the page layout (not inside the refreshable table) so the 5s
    auto-refresh doesn't destroy it. A timer refreshes the log text every second
    while the dialog is open.
    """
    with context.client.layout:
        with ui.dialog() as dialog, ui.card().classes("w-full"):
            dialog.props("maximized")
            with ui.row().classes("items-center justify-between q-mb-md"):
                ui.label("Service Logs").classes("text-h6")
                with ui.row():
                    ui.button("Refresh", icon="refresh", on_click=lambda: _refresh_logs(log_area)).props("flat dense")
                    ui.button("Close", icon="close", on_click=lambda: dialog.close()).props("flat dense")
            log_area = ui.textarea().classes("w-full").props("readonly autogrow").classes("font-mono text-caption")
            log_area.style("height: calc(100vh - 140px)")

            def _refresh_logs(la: Any) -> None:
                la.value = "\n".join(_LOG_BUFFER)

            _refresh_logs(log_area)
            ui.timer(1.0, lambda: _refresh_logs(log_area))
        dialog.open()


_EXAMPLE_TASKS = [
    (
        "Find missing supporting files",
        "Show entities from template DOCUMENT and Document Status to Adopted that has no supporting files",
    ),
    (
        "Extract data from attachments",
        "Fill a property from the attached HTML file",
    ),
    (
        "Merge multilingual duplicates",
        "Merge entities holding same PDF in different languages",
    ),
]


def _capabilities_dialog() -> None:
    """Show example tasks the service can carry out.

    Created on the page layout (not inside the refreshable table) so the 5s
    auto-refresh doesn't destroy it. Mirrors ``_logs_dialog``'s modal layout.
    """
    with context.client.layout:
        with ui.dialog() as dialog, ui.card().classes("w-full"):
            dialog.props("maximized")
            with ui.row().classes("items-center justify-between q-mb-md"):
                ui.label("Example Tasks").classes("text-h6")
                ui.button("Close", icon="close", on_click=lambda: dialog.close()).props("flat dense")
            content = ui.column().classes("w-full q-pr-md").style("height: calc(100vh - 120px); overflow-y: auto")
            with content:
                for title, prompt in _EXAMPLE_TASKS:
                    with ui.card().classes("w-full"):
                        ui.label(title).classes("text-subtitle1 text-bold")
                        ui.label(prompt).classes("text-body2 text-grey-7")
        dialog.open()


def _clear_cache_dialog() -> None:
    """Confirm + clear the persistent file/entity cache (forces fresh reads).

    Created on the page layout (not inside the refreshable table) so the 5s
    auto-refresh doesn't destroy it. Clearing drops every cached entity raw and
    file byte for the configured instance, so the next task re-fetches server
    truth instead of serving stale copies — the fix for results that don't
    change after editing entities directly in Uwazi.
    """
    with context.client.layout:
        with ui.dialog() as dialog, ui.card().classes("w-full max-w-lg"):
            ui.label("Clear cache").classes("text-h6")
            ui.label(
                "Clear the persistent file/entity cache? The next task will "
                "re-fetch entity data and file bytes from Uwazi instead of "
                "serving cached copies. Use this after editing entities "
                "directly in Uwazi so results reflect the latest data."
            ).classes("text-body1")
            with ui.row().classes("w-full justify-end"):
                ui.button("Cancel", on_click=lambda: dialog.close()).props("color=grey-7 flat")
                ui.button("Clear", color="negative", on_click=lambda: _do_clear_cache(dialog))
    dialog.open()


def _do_clear_cache(dialog: Any) -> None:
    """Run the cache clear and report how many entries were removed."""
    dialog.close()
    try:
        removed = clear_cache()
    except Exception as exc:  # noqa: BLE001 — surface every failure to the operator
        ui.notify(f"Failed to clear cache: {exc}", type="negative", multi_line=True)
        return
    ui.notify(f"Cache cleared ({removed} entries removed)", type="positive")


def _confirm_and_close(
    dialog: Any, on_confirm: Any, success_msg: str, run_id: str | None = None, kind: str = "reverting"
) -> None:
    dialog.close()
    # Let the outbox flush the close before the background task starts: the
    # revert/execute run a synchronous Uwazi login first, and blocking the
    # event loop before the flush stalls the modal visibly open.
    ui.timer(0.05, lambda: _start_confirmed_task(on_confirm, success_msg, run_id, kind), once=True)


def _start_confirmed_task(on_confirm: Any, success_msg: str, run_id: str | None = None, kind: str = "reverting") -> None:
    # Claim at the actual launch — a failed claim aborts the whole confirm
    # action with a busy toast and no task (covers the revert path via the
    # confirm dialog and the sync delete path).
    if run_id is not None and not _try_claim(run_id, kind):
        ui.notify(_busy_label(), type="warning")
        return
    try:
        result = on_confirm()
    except Exception:
        # A sync op (delete) that raises before its task exists must still
        # release, or the claim would block every later op until restart.
        if run_id is not None:
            _release_run(run_id)
        raise
    if hasattr(result, "__await__"):
        background_tasks.create(_run_async(result, success_msg, run_id), name=success_msg)
    else:
        ui.notify(success_msg, type="positive")
        if run_id is not None:
            _release_run(run_id)
        _broadcast_rows()


def _new_task_wizard(prefill_name: str = "", prefill_prompt: str = "") -> None:
    """Multi-step dialog to create + generate a run.

    ``prefill_name``/``prefill_prompt`` pre-populate the wizard (used by the
    Duplicate menu action); when a prefill is present the wizard opens
    directly on the Generate review step (validation is skipped).
    """
    state: dict[str, Any] = {"name": prefill_name, "prompt": prefill_prompt, "final_prompt": prefill_prompt}

    with ui.dialog() as dialog, ui.card().classes("w-full max-w-4xl"):
        dialog.props("persistent")
        with ui.stepper() as stepper:
            state["_dialog"] = dialog
            state["_stepper"] = stepper
            _wizard_step_name(state, stepper, dialog, prefill=prefill_name)
            _wizard_step_prompt(state, stepper, dialog, prefill=prefill_prompt)
            _wizard_step_validate(state, stepper, dialog)
            _wizard_step_generate(state, dialog, stepper)
        dialog.open()
        if prefill_name or prefill_prompt:
            stepper.set_value("generate")
            _show_generate_prompt(state)


def _wizard_step_name(state: dict[str, str], stepper: Any, dialog: Any, prefill: str = "") -> None:
    with ui.step(name="name", title="Name", icon="edit"):
        ui.label("Name the run (a new folder will be created under data/runs/).").classes("text-body1 q-mb-md")
        name_input = ui.input(
            "Run name",
            placeholder="e.g. merge-entities-2026",
            value=prefill,
            validation={"Required": lambda v: bool(v and v.strip())},
        ).classes("w-full text-h6")
        with ui.row().classes("q-mt-lg w-full justify-end"):
            ui.button("Cancel", on_click=lambda: dialog.close()).props("color=grey-7 flat")
            ui.button("Next", on_click=lambda: _wizard_name_next(state, name_input, stepper))


def _wizard_name_next(state: dict[str, str], name_input: Any, stepper: Any) -> None:
    value = (name_input.value or "").strip()
    existing = {r.run_id for r in list_runs()}
    if not value:
        ui.notify("Name is required", type="warning")
        return
    if value in existing:
        ui.notify(f"A run named {value!r} already exists", type="warning")
        return
    state["name"] = value
    stepper.set_value("prompt")


def _wizard_step_prompt(state: dict[str, str], stepper: Any, dialog: Any, prefill: str = "") -> None:
    with ui.step(name="prompt", title="Prompt", icon="chat"):
        ui.label("Describe the migration in natural language.").classes("text-body1 q-mb-md")
        prompt_input = (
            ui.textarea(
                "Prompt",
                placeholder="e.g. Merge duplicate entities sharing the title 'X'...",
                value=prefill,
                validation={"Required": lambda v: bool(v and v.strip())},
            )
            .classes("w-full")
            .props("autogrow input-style='min-height: 200px;'")
            .style("width: 100%")
        )
        with ui.row().classes("q-mt-lg w-full justify-end"):
            ui.button("Cancel", on_click=lambda: dialog.close()).props("color=grey-7 flat")
            ui.button("Back", on_click=lambda: stepper.set_value("name")).props("color=grey-7 flat")
            ui.button("Next", on_click=lambda: _wizard_prompt_next(state, prompt_input, stepper))


def _wizard_prompt_next(state: dict[str, Any], prompt_input: Any, stepper: Any) -> None:
    value = (prompt_input.value or "").strip()
    if not value:
        ui.notify("Prompt is required", type="warning")
        return
    state["prompt"] = value
    state["final_prompt"] = value
    state["validated_prompt"] = None
    state["_validation_done"] = False
    _render_validate_idle(state, state["_validate_container"])
    stepper.set_value("validate")


def _wizard_step_validate(state: dict[str, Any], stepper: Any, dialog: Any) -> None:
    with ui.step(name="validate", title="Validate", icon="help"):
        container = ui.column().classes("w-full")
        state["_validate_container"] = container
        _render_validate_idle(state, container)


def _render_validate_idle(state: dict[str, Any], container: Any) -> None:
    """Show the pre-validation state: a Validate button (validation is opt-in)."""
    container.clear()
    with container:
        ui.label("Ask the LLM what it understood from your prompt before generating.").classes("text-body1 q-mb-md")
        with ui.row().classes("q-mt-lg w-full justify-end"):
            ui.button("Cancel", on_click=lambda: state["_dialog"].close()).props("color=grey-7 flat")
            ui.button("Back", on_click=lambda: state["_stepper"].set_value("prompt")).props("color=grey-7 flat")
            ui.button("Skip", on_click=lambda: _wizard_skip(state)).props("color=grey-7 flat")
            ui.button("Validate", icon="help", on_click=lambda: _start_validation(state)).props("color=primary")


def _start_validation(state: dict[str, Any]) -> None:
    """Kick off the prompt-validation LLM call and show a loading state."""
    container = state["_validate_container"]
    container.clear()
    with container:
        with ui.row().classes("w-full items-center gap-2"):
            ui.spinner(size="lg")
            ui.label("Validating prompt — asking the LLM what it understood...").classes("text-body1")
    background_tasks.create(_do_clarify(state, container), name="clarify prompt")


async def _do_clarify(state: dict[str, Any], container: Any) -> None:
    """Run the clarification LLM call on a worker thread, then render the result."""
    prompt = state["prompt"]
    try:
        understanding = await asyncio.to_thread(
            asyncio.run,
            clarify_prompt(prompt, app.storage.user["user"], app.storage.user["password"]),
        )
    except Exception as exc:  # noqa: BLE001
        _render_validation_error(state, container, exc)
        return
    state["understanding"] = understanding
    _render_validation(state, container)


def _render_validation_error(state: dict[str, Any], container: Any, exc: Exception) -> None:
    """Show a validation failure with a skip affordance (the operator can proceed)."""
    state["_validation_done"] = True
    container.clear()
    with container:
        ui.label("Validation failed").classes("text-h6 text-negative")
        ui.label(str(exc)).classes("text-body2 text-red-10 q-mb-md")
        with ui.row().classes("q-mt-lg w-full justify-end"):
            ui.button("Cancel", on_click=lambda: state["_dialog"].close()).props("color=grey-7 flat")
            ui.button("Back", on_click=lambda: state["_stepper"].set_value("prompt")).props("color=grey-7 flat")
            ui.button("Skip", on_click=lambda: _wizard_skip(state)).props("color=primary")


def _render_validation(state: dict[str, Any], container: Any) -> None:
    """Render the LLM's understanding: summary + clarifying questions + notes + preview."""
    state["_validation_done"] = True
    understanding = state["understanding"]
    container.clear()
    with container:
        ui.label("What the LLM understood").classes("text-subtitle1 text-grey-7")
        ui.label(understanding.summary).classes("text-body1 q-mb-md")

        answer_widgets: list[dict[str, Any]] = []
        if understanding.questions:
            ui.label("Clarifying questions").classes("text-subtitle1 text-grey-7 q-mt-md")
            for question in understanding.questions:
                with ui.card().classes("w-full q-mb-sm"):
                    ui.label(question.question).classes("text-body1 text-weight-medium")
                    option_checkboxes = [
                        ui.checkbox(option, on_change=lambda: _refresh_final_prompt(state)) for option in question.options
                    ]
                    custom_checkbox = ui.checkbox("Custom answer", on_change=lambda: _refresh_final_prompt(state))
                    custom_input = ui.input("Your answer", on_change=lambda: _refresh_final_prompt(state)).classes("w-full")
                    answer_widgets.append(
                        {
                            "question": question.question,
                            "options": list(zip(question.options, option_checkboxes)),
                            "custom_checkbox": custom_checkbox,
                            "custom_input": custom_input,
                        }
                    )
        state["_answer_widgets"] = answer_widgets

        ui.label("Additional notes").classes("text-subtitle1 text-grey-7 q-mt-md")
        notes_input = (
            ui.textarea("Notes", on_change=lambda: _refresh_final_prompt(state)).classes("w-full").props("autogrow")
        )
        state["_notes_input"] = notes_input

        ui.label("Final prompt (sent to the LLM)").classes("text-subtitle1 text-grey-7 q-mt-md")
        preview = ui.textarea().classes("w-full font-mono").props("readonly outlined autogrow").style("min-height: 120px")
        state["_preview"] = preview

        with ui.row().classes("q-mt-lg w-full justify-end"):
            ui.button("Cancel", on_click=lambda: state["_dialog"].close()).props("color=grey-7 flat")
            ui.button("Back", on_click=lambda: state["_stepper"].set_value("prompt")).props("color=grey-7 flat")
            ui.button("Skip", on_click=lambda: _wizard_skip(state)).props("color=grey-7 flat")
            ui.button("Confirm", on_click=lambda: _wizard_confirm(state)).props("color=primary")

        _refresh_final_prompt(state)


def _collect_answers(state: dict[str, Any]) -> list[QuestionAnswer]:
    """Read the current checkbox/input values into :class:`QuestionAnswer` objects."""
    answers: list[QuestionAnswer] = []
    for widget in state.get("_answer_widgets", []):
        selected = [option for option, checkbox in widget["options"] if checkbox.value]
        custom = None
        if widget["custom_checkbox"].value and (widget["custom_input"].value or "").strip():
            custom = widget["custom_input"].value.strip()
        answers.append(QuestionAnswer(question=widget["question"], selected=selected, custom=custom))
    return answers


def _refresh_final_prompt(state: dict[str, Any]) -> None:
    """Recompute the final prompt from the current answers + notes and update the preview."""
    notes_input = state.get("_notes_input")
    notes = (notes_input.value or "") if notes_input is not None else ""
    final = build_final_prompt(state["prompt"], _collect_answers(state), notes)
    state["final_prompt"] = final
    preview = state.get("_preview")
    if preview is not None:
        preview.value = final


def _wizard_skip(state: dict[str, Any]) -> None:
    """Proceed to generate with the original prompt (no validation applied)."""
    state["final_prompt"] = state["prompt"]
    state["validated_prompt"] = None
    _show_generate_prompt(state)
    state["_stepper"].set_value("generate")


def _wizard_confirm(state: dict[str, Any]) -> None:
    """Proceed to generate with the validated (final) prompt."""
    _refresh_final_prompt(state)
    state["validated_prompt"] = state["final_prompt"]
    _show_generate_prompt(state)
    state["_stepper"].set_value("generate")


def _show_generate_prompt(state: dict[str, Any]) -> None:
    """Update the generate step's prompt display to the current final prompt."""
    display = state.get("_generate_prompt_display")
    if display is not None:
        display.value = state.get("final_prompt") or state.get("prompt") or ""


def _wizard_generate_back(state: dict[str, Any]) -> None:
    """Back from the generate step: to validate (if it ran) or prompt (duplicate path)."""
    target = "validate" if state.get("_validation_done") else "prompt"
    state["_stepper"].set_value(target)


def _wizard_step_generate(state: dict[str, Any], dialog: Any, stepper: Any) -> None:
    with ui.step(name="generate", title="Generate", icon="auto_awesome"):
        ui.label("Review and generate the migration script.").classes("text-body1 q-mb-md")
        ui.label().bind_text_from(state, "name", backward=lambda v: f"Run: {v}").classes("text-h6")
        ui.label("Final prompt (sent to the LLM)").classes("text-subtitle1 text-grey-7 q-mt-md")
        prompt_display = (
            ui.textarea().classes("w-full font-mono").props("readonly outlined autogrow").style("min-height: 120px")
        )
        state["_generate_prompt_display"] = prompt_display
        with ui.row().classes("q-mt-lg w-full justify-end"):
            ui.button("Cancel", on_click=lambda: dialog.close()).props("color=grey-7 flat")
            ui.button("Back", on_click=lambda: _wizard_generate_back(state)).props("color=grey-7 flat")
            ui.button(
                "Generate",
                icon="auto_awesome",
                on_click=lambda: _wizard_generate(state, dialog),
            )


def _start_generation(
    name: str,
    prompt: str,
    user: str,
    password: str,
    validated_prompt: str | None = None,
    claimed: bool = False,
) -> None:
    """Register the in-flight placeholder + notification and launch generation.

    Shared by the new-task wizard and the retry path on a ``generation_failed``
    run so both produce the identical toast/table/background-task flow.

    ``claimed`` is set by the retry path, which already holds the claim (taken
    before its destructive ``delete_run``); re-claiming here would fail and
    abort generation after the run was already deleted.
    """
    if not claimed and not _try_claim(name, "creating"):
        ui.notify(_busy_label(), type="warning")
        return
    _creating_runs[name] = {"name": name, "prompt": prompt}
    _broadcast_rows()
    _generating_notifications[name] = ui.notification(
        "Generating script (this may take a minute)...",
        type="ongoing",
        spinner=True,
        timeout=None,
    )
    background_tasks.create(
        _do_generate(name, prompt, user, password, validated_prompt),
        name=f"generate {name}",
    )


def _wizard_generate(state: dict[str, Any], dialog: Any) -> None:
    name = state.get("name", "")
    prompt = state.get("prompt", "")
    if not name or not prompt:
        ui.notify("Name and prompt are required", type="warning")
        return
    dialog.close()
    _start_generation(
        name,
        prompt,
        app.storage.user["user"],
        app.storage.user["password"],
        validated_prompt=state.get("validated_prompt"),
    )


async def _do_generate(name: str, prompt: str, user: str, password: str, validated_prompt: str | None = None) -> None:
    # create_and_generate does synchronous Uwazi HTTP + LLM calls; run it on a
    # worker thread so the event loop stays free to serve the UI and websocket.
    try:
        await asyncio.to_thread(asyncio.run, create_and_generate(name, prompt, user, password, validated_prompt))
        _broadcast_notify(f"Run {name!r} created and generated", type="positive")
    except Exception as exc:  # noqa: BLE001
        _notify_error("Generation failed", str(exc))
    finally:
        _creating_runs.pop(name, None)
        _release_run(name)
        notification = _generating_notifications.pop(name, None)
        if notification is not None:
            notification.dismiss()
        _broadcast_rows()


def _build_page() -> None:
    """Build the single-page layout."""
    ui.colors(primary="#2c3e50", secondary="#18bc9c", accent="#f39c12")
    ui.add_head_html(
        "<style>.q-stepper, .q-stepper__header, .q-stepper__step, .q-stepper__step-content, .q-stepper__step-inner, .q-stepper__content, .q-panel { width: 100% !important; max-width: none !important; }</style>"
    )
    with ui.header().classes("items-center justify-between"):
        with ui.row().classes("items-center"):
            ui.label("Uwazi Admin Agent").classes("text-h6 q-mr-md")
        with ui.row().classes("items-center"):
            ui.icon("link", color="secondary").classes("q-mr-xs")
            ui.link(_CONTROLLED_UWAZI_URL, _CONTROLLED_UWAZI_URL, new_tab=True).classes("text-white")
            with ui.row().classes("items-center q-ml-md"):
                new_task_button = ui.button("New Task", icon="add", on_click=_new_task_wizard).props("flat color=secondary")
                context.client._new_task_button = new_task_button  # noqa: SLF001 — per-client busy disable
                with ui.button(icon="menu").props("flat round color=secondary"):
                    with ui.menu():
                        ui.menu_item("Logs", _logs_dialog)
                        ui.menu_item("Capabilities", _capabilities_dialog)
                        ui.menu_item("Clear cache", _clear_cache_dialog)
                        ui.separator()
                        ui.menu_item("Log out", _logout)

    with ui.column().classes("w-full items-center"):
        # Busy banner: text set/visibility toggled per client by
        # _refresh_rows_client while any mutating op is in flight.
        busy_banner = (
            ui.label("")
            .classes("w-full max-w-6xl q-px-md q-py-sm text-body1 text-white bg-warning")
            .style("border-radius: 4px")
            .set_visibility(False)
        )
        context.client._busy_banner = busy_banner  # noqa: SLF001 — per-client busy banner
        with ui.card().classes("w-full max-w-6xl"):
            table = _build_runs_table()
            context.client._runs_table = table  # noqa: SLF001 — per-client handle for in-place refresh
    _build_row_menu()

    ui.timer(5.0, _refresh_rows_client)

    # Prevent the browser back button from navigating away when a dialog is open.
    # Push a sentinel state on page load; when back is pressed while a dialog is
    # open, re-push the state and close the dialog instead of leaving the page.
    ui.add_body_html(
        """
        <script>
        (function() {
          window.history.pushState({nicegui_dialog: true}, '');
          window.addEventListener('popstate', function(e) {
            const hasDialog = document.querySelector('.q-dialog__inner:not([style*="display: none"])');
            if (hasDialog) {
              // A dialog is open: re-push state and close the dialog instead of navigating away.
              window.history.pushState({nicegui_dialog: true}, '');
              // Click the Close or Cancel button inside the dialog.
              const dlg = hasDialog.closest('.q-dialog');
              if (dlg) {
                const closeBtn = [...dlg.querySelectorAll('button')].find(b => {
                  const t = b.textContent.trim().toLowerCase();
                  return t.startsWith('close') || t === 'cancel';
                });
                if (closeBtn) closeBtn.click();
                else hasDialog.click(); // backdrop click as fallback for non-persistent dialogs
              }
            } else {
              // No dialog open: re-push so the user stays on the page.
              window.history.pushState({nicegui_dialog: true}, '');
            }
          });
        })();
        </script>
        """
    )


@ui.page("/")
def _index() -> None:
    """Login gate: nothing renders until the Uwazi credentials validate."""
    _login_page()


@ui.page("/app")
def _app() -> None:
    """Main admin app: only reachable with a validated session login."""
    if not _is_logged_in():
        ui.navigate.to("/")
        return
    _build_page()


def main() -> None:
    """Run the NiceGUI server (uvicorn under the hood) on port 5055."""
    port = int(os.environ.get("ADMIN_WEB_PORT", "5055"))
    ui.run(
        host="0.0.0.0",
        port=port,
        reload=False,
        title="Uwazi Admin Agent",
        storage_secret=os.environ.get("ADMIN_WEB_STORAGE_SECRET", "dev-admin-web-secret"),
    )


if __name__ == "__main__":
    main()
