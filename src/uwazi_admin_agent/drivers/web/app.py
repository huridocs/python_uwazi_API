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
import sys
import threading
from collections import deque
from pathlib import Path
from typing import Any, Callable

from fastapi import Response
from loguru import logger
from nicegui import app, background_tasks, context, ui

from uwazi_admin_agent.configuration import (
    LLM_PRICE_INPUT_PER_MTOK,
    LLM_PRICE_OUTPUT_PER_MTOK,
    SCRIPT_DEFAULT_MAX_COST,
    SCRIPT_DEFAULT_MAX_REQUESTS,
)
from uwazi_admin_agent.domain.execute_gate import ExecuteRefusedError
from uwazi_admin_agent.domain.llm_budget import LlmBudget, LlmPrice
from uwazi_admin_agent.domain.metadata_diff import PropertyChange, build_property_changes
from uwazi_admin_agent.domain.prompt_validation import QuestionAnswer, build_final_prompt
from uwazi_admin_agent.domain.revert_gate import RevertRefusedError

# Import for its side effect: the ``@ui.page("/review/{run_id}")`` route registers
# on import, so the runs table's Review action can navigate to it.
from uwazi_admin_agent.drivers.web import review_page as _review_page_module  # noqa: F401
from uwazi_admin_agent.drivers.web import task_page as _task_page_module  # noqa: F401
from uwazi_admin_agent.drivers.web.run_service import (
    GenerateError,
    RevertVerificationError,
    RunSummary,
    clarify_prompt,
    clear_cache,
    create_and_generate,
    delete_run,
    fetch_entity_document_bytes,
    get_entity_segmentation,
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
    """Short headline toast; the full detail lives in the run's error dialog.

    Also LOGS. Every failure toast is raised from inside an ``except`` block of a
    NiceGUI background task, whose exceptions never reach the uvicorn error
    handler — so without this the container terminal stayed silent and the only
    trace of a failed generation was a toast that vanished with the page. Loguru
    writes to stderr (the ``docker logs`` stream) AND to the ring buffer behind the
    in-app Logs dialog, and ``logger.exception`` attaches the traceback while one
    is active, which is what makes the failure debuggable from the terminal alone.
    """
    first_line = (detail or "").strip().splitlines()[0] if (detail or "").strip() else title
    if sys.exc_info()[0] is not None:
        logger.exception("{}: {}", title, detail)
    else:
        logger.error("{}: {}", title, detail)
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
            <q-btn dense flat icon="fact_check" color="secondary" title="Review &amp; apply"
                   @click="$parent.$emit('review', props.row)" />
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

    table.on("review", _on_review)
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
            ui.menu_item("Review suggestions", lambda: _row_menu_action(_rowmenu_review))
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


def _rowmenu_review(run_id: str) -> None:
    """Jump to the run's review page."""
    ui.navigate.to(f"/review/{run_id}")


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


def _on_rollback(e: Any) -> None:
    run_id = e.args["name"] if isinstance(e.args, dict) else e.args
    _confirm_dialog(
        "Rollback run",
        f"Revert run {run_id!r}? This restores every backed-up entity and deletes created ones.",
        lambda: revert_run(run_id, app.storage.user["user"], app.storage.user["password"]),
        success_msg=f"Reverted {run_id}",
        run_id=run_id,
    )


def _on_review(e: Any) -> None:
    """Open the run's review page — the primary write path (accept writes there)."""
    run_id = e.args["name"] if isinstance(e.args, dict) else e.args
    ui.navigate.to(f"/review/{run_id}")


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


def _close_button(dialog: Any, dense: bool = False) -> Any:
    """Standard modal dismiss button (X icon + label), flat."""
    button = ui.button("Close", icon="close", on_click=dialog.close).props("flat")
    if dense:
        button.props("dense")
    return button


def _modal_header(dialog: Any, title: str, trailing: Callable[[], Any] | None = None) -> None:
    """Modal title row with a top-right Close button.

    Every modal keeps its Close at the BOTTOM-right too (pinned footers in the
    maximized dialogs); this is the second, always-reachable escape hatch at the
    top. ``trailing`` builds extra header widgets (e.g. Refresh / Copy all) that
    sit between the title and the Close button.

    The row is ``shrink-0``: the maximized cards are fixed-height flex columns
    whose middle section scrolls, and a compressible header would be squeezed
    to nothing once the scroll content stops shrinking (``shrink-0`` children).
    """
    with ui.row().classes("w-full items-center justify-between no-wrap shrink-0"):
        ui.label(title).classes("text-h6")
        with ui.row().classes("items-center no-wrap"):
            if trailing is not None:
                trailing()
            _close_button(dialog)


def _footer_close(dialog: Any) -> None:
    """The bottom-right Close row that closes a maximized modal.

    ``shrink-0`` keeps it pinned: only the middle scroll column may shrink.
    """
    with ui.row().classes("w-full justify-end shrink-0"):
        ui.button("Close", icon="close", on_click=dialog.close).props("flat")


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
            _modal_header(dialog, title)
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
            _modal_header(dialog, f"Rename task — {run_id}")
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

    The title and the Close footer stay pinned; the middle column scrolls.
    Its children are all ``shrink-0`` for the same reason as
    ``_script_dialog``: compressible children squeeze the autogrow textareas'
    boxes while their content keeps its full height, so the prompt text spills
    out of the textbox and overlaps the widgets below it.
    """
    try:
        detail = get_run(run_id)
    except Exception as exc:  # noqa: BLE001
        ui.notify(f"Failed to load run: {exc}", type="negative", multi_line=True)
        return

    with context.client.layout:
        with ui.dialog() as dialog, ui.card().classes("w-full max-w-2xl max-h-[85vh]").props("style=height:85dvh"):
            with ui.column().classes("w-full h-full items-stretch no-wrap"):
                _modal_header(dialog, f"Task — {run_id}")
                ui.separator().classes("shrink-0")
                with ui.column().classes("w-full grow").style("overflow-y: auto; min-height: 0"):
                    with ui.row().classes("w-full items-center justify-between shrink-0"):
                        ui.label("Prompt").classes("text-subtitle1 text-grey-7")
                        _copy_to_clipboard(detail.prompt or "", "Copy prompt")
                    ui.textarea(value=detail.prompt or "").classes("w-full font-mono shrink-0").props(
                        "readonly outlined autogrow"
                    ).style("min-height: 120px")
                    if detail.validated_prompt:
                        ui.separator().classes("shrink-0")
                        with ui.row().classes("w-full items-center justify-between shrink-0"):
                            ui.label("Validated prompt").classes("text-subtitle1 text-grey-7")
                            _copy_to_clipboard(detail.validated_prompt, "Copy validated prompt")
                        ui.textarea(value=detail.validated_prompt).classes("w-full font-mono shrink-0").props(
                            "readonly outlined autogrow"
                        ).style("min-height: 120px")
                    if detail.error:
                        ui.separator().classes("shrink-0")
                        ui.label("Last error").classes("text-subtitle1 text-grey-7 shrink-0")
                        ui.label(detail.error.strip().splitlines()[0]).classes("text-body2 text-red-10 shrink-0")
                _footer_close(dialog)
    dialog.open()


def _history_dialog(run_id: str) -> None:
    """Modal: a run's execute/revert history (time, type, outcome).

    Created on the page layout (not inside the refreshable table) so the 5s
    auto-refresh doesn't destroy it. Reads the run's audit log via
    ``get_execution_history``; a run with no executions shows an empty state.
    """
    events = get_execution_history(run_id)
    with context.client.layout:
        with ui.dialog() as dialog, ui.card().classes("w-full max-w-2xl max-h-[85vh]").props("style=height:85dvh"):
            with ui.column().classes("w-full h-full items-stretch no-wrap"):
                _modal_header(dialog, f"Execution history — {run_id}")
                with ui.column().classes("w-full grow").style("overflow-y: auto; min-height: 0"):
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
                _footer_close(dialog)
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
                _modal_header(dialog, f"Error details — {run_id}")
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
                _footer_close(dialog)
    dialog.open()


def _script_dialog(run_id: str) -> None:
    """Modal: the run's generated Python script (only reachable when a script exists).

    Created on the page layout (not inside the refreshable table) so the 5s
    auto-refresh doesn't destroy it — same pattern as ``_error_dialog``. The
    run's generated script lives at ``<run>/script.py``; a run whose generation
    failed has none, and the menu item is hidden in that case.

    The dialog is maximized: the title row and the Close footer stay pinned
    while the middle column scrolls. Every child of that scrolling column is
    ``shrink-0`` — a compressible child lets the autogrow prompt textarea (and
    the ``ui.code`` block, whose inner markdown is sized ``height: 100%``) keep
    its full content height while its box is squeezed, so the script text
    spills out of its box and paints on top of the prompt textbox. With nothing
    shrinkable the column simply scrolls instead.
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
        with ui.dialog() as dialog, ui.card().classes("w-full").props("style=height:100dvh"):
            dialog.props("maximized")
            with ui.column().classes("w-full h-full items-stretch no-wrap"):
                _modal_header(
                    dialog,
                    f"Generated script — {run_id}",
                    lambda: _copy_to_clipboard(
                        f"Prompt:\n{detail.prompt or ''}\n\n"
                        f"Validated prompt:\n{detail.validated_prompt or ''}\n\n"
                        f"Script:\n{detail.script}",
                        "Copy all",
                    ),
                )
                with ui.column().classes("w-full grow").style("overflow-y: auto; min-height: 0"):
                    with ui.row().classes("w-full items-center justify-between shrink-0"):
                        ui.label("Prompt").classes("text-subtitle1 text-grey-7")
                        _copy_to_clipboard(detail.prompt or "", "Copy prompt")
                    ui.textarea(value=detail.prompt or "").classes("w-full font-mono shrink-0").props(
                        "readonly outlined autogrow"
                    ).style("min-height: 80px")
                    if detail.validated_prompt:
                        with ui.row().classes("w-full items-center justify-between shrink-0"):
                            ui.label("Validated prompt").classes("text-subtitle1 text-grey-7")
                            _copy_to_clipboard(detail.validated_prompt, "Copy validated prompt")
                        ui.textarea(value=detail.validated_prompt).classes("w-full font-mono shrink-0").props(
                            "readonly outlined autogrow"
                        ).style("min-height: 80px")
                    with ui.row().classes("w-full items-center justify-between shrink-0"):
                        ui.label("Script").classes("text-subtitle1 text-grey-7")
                        _copy_to_clipboard(detail.script, "Copy script")
                    ui.code(detail.script, language="python").classes("w-full shrink-0")
                _footer_close(dialog)
    dialog.open()


def _format_value(value: Any) -> str:
    """Render an arbitrary manifest value (e.g. a rewired 'before' state) as text."""
    if value is None:
        return "—"
    text = str(value)
    return text if len(text) <= 200 else text[:200] + "…"


def _entity_url(shared_id: str, language: str | None) -> str | None:
    """The controlled instance's public URL for one entity row, or None if unknown.

    Uwazi serves an entity at ``/<language>/entity/<sharedId>``; the language
    falls back to ``en`` when the row's locale is unknown. Returns None when
    ``UWAZI_URL`` is not configured, so the caller omits the link instead of
    rendering a broken one.
    """
    if _CONTROLLED_UWAZI_URL == "not configured":
        return None
    return f"{_CONTROLLED_UWAZI_URL.rstrip('/')}/{language or 'en'}/entity/{shared_id}"


def _merge_changes(writes: list[Any]) -> list[PropertyChange]:
    """Merge one entity's applied writes into one property list (record order).

    A property touched by several writes keeps the BEFORE of the first write
    that touched it (the pre-run value) and the AFTER of the last (the surviving
    value), so the table shows the net effect of the run rather than each step.
    """
    order: list[str] = []
    before: dict[str, str] = {}
    after: dict[str, str] = {}
    for write in writes:
        for change in build_property_changes(write):
            if change.property not in before:
                order.append(change.property)
                before[change.property] = change.before
            after[change.property] = change.after
    return [
        PropertyChange(
            property=name,
            before=before[name],
            after=after[name],
            changed=before[name] != after[name],
        )
        for name in order
    ]


def _change_summary(changes: list[PropertyChange]) -> str:
    """One-line summary of the changed properties, truncated with a remainder count."""
    changed = [c.property for c in changes if c.changed]
    if not changed:
        return "—"
    shown: list[str] = []
    for name in changed:
        if shown and len(", ".join([*shown, name])) > 60:
            break
        shown.append(name)
    remaining = len(changed) - len(shown)
    text = ", ".join(shown)
    return f"{text} (+{remaining} more)" if remaining else text


def _row_language(identity_language: str | None, writes: list[Any]) -> str | None:
    """The row locale to use for an entity's link and segmentation fetch.

    The identity's language wins. A created entity's identity carries none
    (Uwazi mints the row; we only learn its sharedId), so the write that created
    it supplies the language the script asked for. None when neither knows.
    """
    if identity_language:
        return identity_language
    return next((w.language for w in writes if w.language), None)


def _entity_section(
    title: str,
    entities: list[Any],
    writes_by_sid: dict[str, list[Any]],
    titles: dict[str, str],
    include_restored: bool = False,
    show_changes: bool = True,
    show_segmentation: bool = True,
) -> Any:
    """Render one touch-set section (modified/created/deleted) as a table, if non-empty.

    Rows prefer the entity title over the shared id, carry the controlled
    instance's URL for the open-in-Uwazi icon, and summarize the properties the
    run changed for that entity. The emitted events (``changes``,
    ``segmentation``) are wired by the caller, which owns the run's data.

    ``show_changes`` is off for deleted entities (the run removed their metadata
    with the row) and ``show_segmentation`` likewise (their stored files are
    gone, so the fetch could only fail).

    Rows are paginated 25 per page: a run can touch thousands of entities, and
    rendering them all at once produces a table tall enough to stall the modal
    (and a refresh payload large enough to drop the websocket).
    """
    if not entities:
        return None
    ui.label(title).classes("text-subtitle1 q-mt-md")
    columns = [
        {
            "name": "title",
            "label": "Title",
            "field": "title",
            "align": "left",
            "sortable": True,
            "style": "max-width: 320px; white-space: normal; word-break: break-word",
        },
    ]
    if show_changes:
        columns.append({"name": "changes", "label": "Changes", "field": "changes", "align": "left", "sortable": True})
    columns.append({"name": "language", "label": "Language", "field": "language", "align": "left", "sortable": True})
    if show_segmentation:
        columns.append(
            {"name": "segmentation", "label": "Segmentation", "field": "segmentation", "align": "center", "sortable": False}
        )
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
    columns.append({"name": "shared_id", "label": "Shared ID", "field": "shared_id", "align": "left", "sortable": True})

    rows = []
    for entity in entities:
        shared_id = entity.shared_id
        writes = writes_by_sid.get(shared_id, [])
        changes = _merge_changes(writes)
        language = _row_language(entity.language, writes)
        row = {
            "shared_id": shared_id,
            "title": titles.get(shared_id) or shared_id,
            "url": _entity_url(shared_id, language),
            "language": language or "—",
            "language_raw": language or "en",
            "changes": _change_summary(changes) if show_changes else "—",
            "has_changes": any(c.changed for c in changes),
        }
        if include_restored:
            row["restored_shared_id"] = entity.restored_shared_id or "—"
        rows.append(row)

    table = ui.table(
        rows=rows,
        columns=columns,
        row_key="shared_id",
        pagination={"rowsPerPage": 25},
    ).classes("w-full")
    table.add_slot(
        "body-cell-title",
        """
        <q-td :props="props">
            <span>{{ props.row.title }}</span>
            <a v-if="props.row.url" :href="props.row.url" target="_blank" rel="noopener"
               style="margin-left:8px" title="Open in Uwazi">
                <q-icon name="open_in_new" size="16px" />
            </a>
        </q-td>
        """,
    )
    if show_changes:
        table.add_slot(
            "body-cell-changes",
            """
            <q-td :props="props">
                <span>{{ props.row.changes }}</span>
                <q-btn v-if="props.row.has_changes" dense flat icon="difference" color="primary"
                       title="Show changed properties"
                       @click="$parent.$emit('changes', props.row)" />
            </q-td>
            """,
        )
    if show_segmentation:
        table.add_slot(
            "body-cell-segmentation",
            """
            <q-td :props="props" class="text-no-wrap">
                <q-btn dense flat icon="description" color="grey-8"
                       title="View document segmentation"
                       @click="$parent.$emit('segmentation', props.row)" />
            </q-td>
            """,
        )
    return table


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


def _changes_dialog(row: dict[str, Any], changes: list[PropertyChange]) -> None:
    """Modal: one entity's property changes (before → after), from run data only.

    Opened from a row's diff button; the rows were computed when the results
    dialog was built, so this is pure rendering — no fetch, no I/O.
    """
    with context.client.layout:
        with ui.dialog() as dialog, ui.card().classes("w-full").style("min-width: 600px; height: 85dvh"):
            with ui.column().classes("w-full h-full items-stretch no-wrap"):
                _modal_header(dialog, f"Changes — {row['title']}")
                ui.label(f"sharedId {row['shared_id']}").classes("text-caption text-grey-7")
                columns = [
                    {"name": "property", "label": "Property", "field": "property", "align": "left", "sortable": True},
                    {"name": "before", "label": "Before", "field": "before", "align": "left", "sortable": False},
                    {"name": "after", "label": "After", "field": "after", "align": "left", "sortable": False},
                ]
                rows = [
                    {
                        "property": c.property,
                        "before": c.before,
                        "after": c.after,
                        "changed": c.changed,
                        "cls": "text-weight-medium" if c.changed else "text-grey-7",
                    }
                    for c in changes
                ]
                with ui.column().classes("w-full grow").style("overflow-y: auto; min-height: 0"):
                    table = ui.table(rows=rows, columns=columns, pagination={"rowsPerPage": 0}).classes("w-full")
                    table.add_slot(
                        "body-cell-property",
                        """
                        <q-td :props="props">
                            <span :class="props.row.cls">{{ props.row.property }}</span>
                        </q-td>
                        """,
                    )
                    table.add_slot(
                        "body-cell-after",
                        """
                        <q-td :props="props">
                            <span :class="props.row.cls">{{ props.row.after }}</span>
                        </q-td>
                        """,
                    )
                _footer_close(dialog)
    dialog.open()


def _segmentation_dialog(row: dict[str, Any]) -> None:
    """Modal: one entity's primary-document segmentation, fetched on demand.

    The fetch is the only live Uwazi call this dialog makes (nothing in the run
    data carries document text). A placeholder shows while the background task
    runs; failure degrades to an error label rather than a crash.
    """
    with context.client.layout:
        with ui.dialog() as dialog, ui.card().classes("w-full").props("style=height:100dvh"):
            dialog.props("maximized")
            with ui.column().classes("w-full h-full items-stretch no-wrap"):
                _modal_header(dialog, f"Document segmentation — {row['title']}")
                body = ui.column().classes("w-full grow").style("overflow-y: auto")
                with body:
                    ui.linear_progress(show_value=False).classes("w-full")
                    ui.label(f"Fetching segmentation for {row['shared_id']} ({row['language_raw']})…").classes(
                        "text-body2 text-grey-7"
                    )
                _footer_close(dialog)
                background_tasks.create(_load_segmentation(body, row), name=f"segmentation {row['shared_id']}")
    dialog.open()


async def _load_segmentation(body: Any, row: dict[str, Any]) -> None:
    """Fetch + render one entity's segmentation into an already-open dialog body."""
    user = app.storage.user.get("user")
    password = app.storage.user.get("password")
    if not user or not password:
        _render_segmentation_error(body, RuntimeError("not logged in to Uwazi"))
        return
    try:
        segmentation = await get_entity_segmentation(user, password, row["shared_id"], row["language_raw"])
    except Exception as exc:  # noqa: BLE001 — degrade to an error label, never crash the UI
        _render_segmentation_error(body, exc)
        return
    body.clear()
    with body:
        page_count = max((p.page_number for p in segmentation.paragraphs), default=0)
        ui.label(
            f"{segmentation.filename} — status={segmentation.status}, "
            f"pages={page_count}, paragraphs={len(segmentation.paragraphs)}"
        ).classes("text-subtitle1 text-grey-7")
        ordered = sorted(segmentation.paragraphs, key=lambda p: (p.page_number, p.top, p.left))
        text = "\n".join(f"[page {p.page_number}] {p.text}" for p in ordered) or "(no paragraphs)"
        ui.code(text).classes("w-full").style("white-space: pre-wrap")


def _render_segmentation_error(body: Any, exc: Exception) -> None:
    """Replace the dialog body with a readable failure (the tool's degrade-to-string spirit)."""
    body.clear()
    with body:
        ui.label(f"{type(exc).__name__}: {exc}").classes("text-body2 text-red-10").style("white-space: pre-wrap")


def _results_dialog(run_id: str) -> None:
    """Modal: the run's execution results — its `result` value and touch-set.

    Created on the page layout (not inside the refreshable table) so the 5s
    auto-refresh doesn't destroy it — same pattern as ``_script_dialog``. Reads
    the manifest's touch-set via ``get_run_results``; a run with no touch-set
    (never executed) shows an empty state. The script's `result` value (the
    answer for a query task, or the summary for a mutation task) is shown first.

    Each entity row shows its title (with an open-in-Uwazi icon), a summary of
    the properties the run changed (with a button opening the before/after list),
    the row language, and a button that fetches that entity's document
    segmentation on demand. Titles and diffs come from run data only; the
    segmentation is the single live fetch, and only when asked for.

    The dialog is maximized (top to bottom): the result + summary + section
    tables fill the space between a pinned title and a pinned footer, scrolling
    internally so the Close button is always visible.
    """
    try:
        results = get_run_results(run_id)
    except Exception as exc:  # noqa: BLE001
        ui.notify(f"Failed to load run: {exc}", type="negative", multi_line=True)
        return

    writes_by_sid: dict[str, list[Any]] = {}
    for write in results.applied_writes:
        writes_by_sid.setdefault(write.shared_id, []).append(write)

    def _on_changes(e: Any) -> None:
        row = e.args
        changes = _merge_changes(writes_by_sid.get(row["shared_id"], []))
        if not changes:
            ui.notify("No property changes recorded for this entity.", type="warning")
            return
        _changes_dialog(row, changes)

    def _on_segmentation(e: Any) -> None:
        _segmentation_dialog(e.args)

    def _section(title: str, entities: list[Any], **kwargs: Any) -> None:
        table = _entity_section(title, entities, writes_by_sid, results.titles, **kwargs)
        if table is None:
            return
        table.on("changes", _on_changes)
        table.on("segmentation", _on_segmentation)

    with context.client.layout:
        with ui.dialog() as dialog, ui.card().classes("w-full").props("style=height:100dvh"):
            dialog.props("maximized")
            with ui.column().classes("w-full h-full items-stretch no-wrap"):
                _modal_header(dialog, f"Run results — {run_id}")
                with ui.column().classes("w-full grow").style("overflow-y: auto; min-height: 0"):
                    if results.result is not None:
                        ui.label("Result").classes("text-subtitle1 text-grey-7 shrink-0")
                        ui.label(results.result).classes(
                            "w-full font-mono text-body2 rounded-borders q-pa-sm shrink-0"
                        ).style(
                            "white-space: pre-wrap; word-break: break-word; "
                            "max-height: 320px; overflow-y: auto; background: rgba(0,0,0,0.03)"
                        )
                        ui.separator().classes("shrink-0")
                    counts = [
                        ("Modified", len(results.modified)),
                        ("Created", len(results.created)),
                        ("Deleted", len(results.deleted)),
                        ("Rewired", len(results.rewired)),
                        ("Files deleted", len(results.deleted_files)),
                    ]
                    with ui.row().classes("w-full q-mb-md gap-3 no-wrap items-stretch shrink-0"):
                        for label, count in counts:
                            with (
                                ui.column()
                                .classes("items-center justify-center grow rounded-borders q-pa-sm")
                                .style("border: 1px solid rgba(0,0,0,0.12); min-width: 0")
                            ):
                                ui.label(str(count)).classes("text-h5")
                                ui.label(label).classes("text-caption text-grey-7 text-center")
                    if not any(c for _, c in counts) and results.result is None:
                        ui.label("No results recorded on this run.").classes("text-grey-7 q-pa-md")
                    _section("Modified entities", results.modified)
                    _section("Created entities", results.created)
                    _section(
                        "Deleted entities",
                        results.deleted,
                        include_restored=True,
                        show_changes=False,
                        show_segmentation=False,
                    )
                    _rewired_section(results.rewired)
                    _files_section(results.deleted_files)
                _footer_close(dialog)
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
        with ui.dialog() as dialog, ui.card().classes("w-full").props("style=height:100dvh"):
            dialog.props("maximized")
            with ui.column().classes("w-full h-full items-stretch no-wrap"):
                with ui.row().classes("w-full items-center justify-between q-mb-md"):
                    ui.label("Service Logs").classes("text-h6")
                    with ui.row().classes("items-center no-wrap"):
                        ui.button("Refresh", icon="refresh", on_click=lambda: _refresh_logs(log_area)).props("flat dense")
                        _close_button(dialog, dense=True)
                log_area = (
                    ui.textarea().classes("w-full grow font-mono text-caption").props("readonly").style("min-height: 0")
                )

                def _refresh_logs(la: Any) -> None:
                    la.value = "\n".join(_LOG_BUFFER)

                _refresh_logs(log_area)
                ui.timer(1.0, lambda: _refresh_logs(log_area))
                _footer_close(dialog)
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
        with ui.dialog() as dialog, ui.card().classes("w-full").props("style=height:100dvh"):
            dialog.props("maximized")
            with ui.column().classes("w-full h-full items-stretch no-wrap"):
                with ui.row().classes("w-full items-center justify-between q-mb-md"):
                    ui.label("Example Tasks").classes("text-h6")
                    _close_button(dialog, dense=True)
                with ui.column().classes("w-full grow q-pr-md").style("overflow-y: auto"):
                    for title, prompt in _EXAMPLE_TASKS:
                        with ui.card().classes("w-full"):
                            ui.label(title).classes("text-subtitle1 text-bold")
                            ui.label(prompt).classes("text-body2 text-grey-7")
                _footer_close(dialog)
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
            _modal_header(dialog, "Clear cache")
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
        _modal_header(dialog, "New task")
        with ui.stepper() as stepper:
            state["_dialog"] = dialog
            state["_stepper"] = stepper
            _wizard_step_details(state, stepper, dialog, prefill_name=prefill_name, prefill_prompt=prefill_prompt)
            _wizard_step_validate(state, stepper, dialog)
            _wizard_step_generate(state, dialog, stepper)
        _footer_close(dialog)
        dialog.open()
        if prefill_name or prefill_prompt:
            stepper.set_value("generate")
            _show_generate_prompt(state)


def _wizard_step_details(
    state: dict[str, Any],
    stepper: Any,
    dialog: Any,
    prefill_name: str = "",
    prefill_prompt: str = "",
) -> None:
    """Step 1: the run's name and its prompt, both on one panel.

    The two inputs are shown together (rather than as two consecutive steps)
    because neither is useful without the other: Next validates both and only
    then advances, so an incomplete pair is caught before the validate step.
    """
    with ui.step(name="details", title="Task", icon="edit"):
        ui.label("Name the run (a new folder will be created under data/runs/) and describe the migration.").classes(
            "text-body1 q-mb-md"
        )
        name_input = ui.input(
            "Run name",
            placeholder="e.g. merge-entities-2026",
            value=prefill_name,
            validation={"Required": lambda v: bool(v and v.strip())},
        ).classes("w-full text-h6")
        prompt_input = (
            ui.textarea(
                "Prompt",
                placeholder="e.g. Merge duplicate entities sharing the title 'X'...",
                value=prefill_prompt,
                validation={"Required": lambda v: bool(v and v.strip())},
            )
            .classes("w-full q-mt-md")
            .props("autogrow input-style='min-height: 200px;'")
            .style("width: 100%")
        )
        with ui.row().classes("q-mt-lg w-full justify-end"):
            ui.button("Cancel", on_click=lambda: dialog.close()).props("color=grey-7 flat")
            ui.button("Next", on_click=lambda: _wizard_details_next(state, name_input, prompt_input, stepper))


def _wizard_details_next(state: dict[str, Any], name_input: Any, prompt_input: Any, stepper: Any) -> None:
    """Validate both inputs, store them, and advance to the validate step."""
    name = (name_input.value or "").strip()
    if not name:
        ui.notify("Name is required", type="warning")
        return
    if name in {r.run_id for r in list_runs()}:
        ui.notify(f"A run named {name!r} already exists", type="warning")
        return
    prompt = (prompt_input.value or "").strip()
    if not prompt:
        ui.notify("Prompt is required", type="warning")
        return
    state["name"] = name
    state["prompt"] = prompt
    state["final_prompt"] = prompt
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
            ui.button("Back", on_click=lambda: state["_stepper"].set_value("details")).props("color=grey-7 flat")
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
            ui.button("Back", on_click=lambda: state["_stepper"].set_value("details")).props("color=grey-7 flat")
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
            ui.button("Back", on_click=lambda: state["_stepper"].set_value("details")).props("color=grey-7 flat")
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
    """Back from the generate step: to validate (if it ran) or the details step."""
    target = "validate" if state.get("_validation_done") else "details"
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
        ui.separator()
        with ui.column().classes("w-full q-mt-sm"):
            ui.label("LLM caps for the script's tool calls").classes("text-subtitle1 text-bold")
            ui.label(
                "The generated script may call the LLM to compute values (per entity). "
                "These HARD caps apply ONLY to those calls — never to script generation itself. "
                "A script that computes everything in pure Python costs nothing under them."
            ).classes("text-body2 text-grey-7")
            with ui.row().classes("w-full items-center gap-2"):
                state["_caps_cost_input"] = ui.number(
                    "Max cost ($)", value=SCRIPT_DEFAULT_MAX_COST, min=0.0, step=0.5, format="%.2f"
                ).classes("grow")
                state["_caps_requests_input"] = ui.number(
                    "Max calls (backstop)", value=SCRIPT_DEFAULT_MAX_REQUESTS, min=0, step=1
                ).classes("grow")
            ui.label(
                "Default is 0 on both: the script's LLM calls are disabled — the pass aborts cleanly on the "
                "first one. Raise a cap here to let the script spend within it."
            ).classes("text-body2 text-grey-7")
            ui.label("The pass aborts cleanly at the caps; rows computed before the halt are kept.").classes(
                "text-body2 text-grey-7"
            )
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
    budget: LlmBudget | None = None,
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
        "Generating script, then the automatic LLM-free selection scan (this may take a minute)...",
        type="ongoing",
        spinner=True,
        timeout=None,
    )
    background_tasks.create(
        _do_generate(name, prompt, user, password, validated_prompt, budget),
        name=f"generate {name}",
    )


def _wizard_generate(state: dict[str, Any], dialog: Any) -> None:
    name = state.get("name", "")
    prompt = state.get("prompt", "")
    if not name or not prompt:
        ui.notify("Name and prompt are required", type="warning")
        return
    budget = _wizard_budget(state)
    dialog.close()
    _confirm_generation(name, prompt, state.get("validated_prompt"), budget)


def _script_caps_model() -> str:
    """The configured model name (falls back to a placeholder when unreadable)."""
    try:
        from uwazi_agent.configuration import MODEL

        return MODEL
    except Exception:  # noqa: BLE001 — a missing/renamed constant must not break the wizard
        return "(model not configured)"


def _wizard_budget(state: dict[str, Any]) -> LlmBudget:
    """The approved budget for the script's llm_complete/llm_map calls (wizard caps step).

    0 is a real, operator-visible value — the deny-by-default prefill — so it
    is kept as-is (a 0 request cap aborts the pass on its first LLM call).
    Only an empty/invalid input falls back to the configured default.
    """
    cost = _cap_float(state["_caps_cost_input"].value) if state.get("_caps_cost_input") is not None else None
    requests = _cap_int(state["_caps_requests_input"].value) if state.get("_caps_requests_input") is not None else None
    return LlmBudget(
        max_cost=SCRIPT_DEFAULT_MAX_COST if cost is None else cost,
        max_requests=SCRIPT_DEFAULT_MAX_REQUESTS if requests is None else requests,
        price=LlmPrice(
            model=_script_caps_model(),
            input_per_mtok=LLM_PRICE_INPUT_PER_MTOK,
            output_per_mtok=LLM_PRICE_OUTPUT_PER_MTOK,
        ),
    )


def _cap_float(value: Any) -> float | None:
    """Coerce a caps input to a non-negative float (None/invalid → None = 'use the default')."""
    try:
        parsed = float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return parsed if parsed is not None and parsed >= 0 else None


def _cap_int(value: Any) -> int | None:
    """Coerce a caps input to a non-negative int (None/invalid → None = 'use the default')."""
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _confirm_generation(name: str, prompt: str, validated_prompt: str | None, budget: LlmBudget) -> None:
    """A plain go/no-go confirm, then launch generation.

    Generation itself is bounded by the built-in caps (``MAX_LLM_CALLS``
    requests via pydantic-ai's ``UsageLimits``, ``LLM_MAX_OUTPUT_TOKENS`` per
    call). ``budget`` — approved in the wizard's caps step — is the HARD ceiling
    on the generated script's own llm_complete/llm_map calls, enforced during
    the planning pass that populates the review table.
    """
    ui.notification(
        "Generating the script — the planning pass runs right after and fills the review table under the approved LLM caps.",
        type="info",
        timeout=6,
    )
    _start_generation(
        name,
        prompt,
        app.storage.user["user"],
        app.storage.user["password"],
        validated_prompt=validated_prompt,
        budget=budget,
    )


async def _do_generate(
    name: str,
    prompt: str,
    user: str,
    password: str,
    validated_prompt: str | None = None,
    budget: LlmBudget | None = None,
) -> None:
    # create_and_generate does synchronous Uwazi HTTP + LLM calls; run it on a
    # worker thread so the event loop stays free to serve the UI and websocket.
    try:
        await asyncio.to_thread(asyncio.run, create_and_generate(name, prompt, user, password, validated_prompt, budget))
        _broadcast_notify(
            f"Run {name!r} generated — the planning pass filled the review table.",
            type="positive",
        )
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
    _register_static_and_routes()
    port = int(os.environ.get("ADMIN_WEB_PORT", "5055"))
    ui.run(
        host="0.0.0.0",
        port=port,
        reload=False,
        title="Uwazi Admin Agent",
        storage_secret=os.environ.get("ADMIN_WEB_STORAGE_SECRET", "dev-admin-web-secret"),
    )


_STATIC_DIR = Path(__file__).parent / "static"


def _register_static_and_routes() -> None:
    """Serve the pdf.js viewer assets and the per-entity document route.

    The viewer is the proven pdf.js setup copied from ``uwazi_property_filler``
    (its own static dir and module scripts), pointed at this app's route: the
    review window's iframe loads ``/static/pdfjs/review_viewer.html?src=…``
    with this route already URL-encoded inside, which streams the entity's
    PRIMARY document bytes through the cached file repository. ``lang`` is the
    row's language; the resolver falls back to the instance's other configured
    languages, since an entity often holds its PDF in another one.
    """
    _STATIC_DIR.mkdir(parents=True, exist_ok=True)
    app.add_static_files("/static", str(_STATIC_DIR))

    @app.get("/review-pdf")
    def _review_pdf(run: str, sid: str, lang: str | None = None) -> Response:
        # The viewer loads this route in an iframe, so the browser presents the
        # session cookie: authentication comes from the session, never the URL.
        user: str | None = None
        password: str | None = None
        try:
            user = app.storage.user.get("user")
            password = app.storage.user.get("password")
        except Exception as exc:  # noqa: BLE001 — no session ⇒ unauthenticated, not a crash
            logger.warning("review-pdf session read failed run={} sid={}: {}", run, sid, exc)
        if not user or not password or not sid:
            return Response(status_code=400 if not sid else 401)
        try:
            # ``lang`` is the row's language; the resolver falls back to the
            # instance's other configured languages when the entity carries no
            # document in that one.
            data = asyncio.run(fetch_entity_document_bytes(user, password, sid, lang))
        except Exception as exc:  # noqa: BLE001 — a fetch failure is a 404 to the viewer
            logger.warning("review-pdf fetch failed run={} sid={} lang={}: {}", run, sid, lang, exc)
            return Response(status_code=404)
        if data is None:
            return Response(status_code=404)
        return Response(content=data, media_type="application/pdf")


if __name__ == "__main__":
    main()
