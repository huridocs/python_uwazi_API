"""NiceGUI web UI for the admin agent — a driver replacing the CLI for common ops.

Single page: a table of runs with status badges + row actions (execute, rollback,
view prompt, and a "more" menu with delete + rename), and a "New Task" wizard
(stepper) to create + generate a run. Mutating operations run as background
tasks; the table auto-refreshes every 5s.

This is a driver: it wires the service layer (:mod:`run_service`) to the UI and
contains no business logic, matching the ``drivers/`` layer convention.
"""

import asyncio
import os
from collections import deque
from typing import Any

from loguru import logger
from nicegui import app, background_tasks, context, ui

from uwazi_admin_agent.domain.execute_gate import ExecuteRefusedError
from uwazi_admin_agent.domain.revert_gate import RevertRefusedError
from uwazi_admin_agent.drivers.web.run_service import (
    GenerateError,
    RevertVerificationError,
    RunSummary,
    create_and_generate,
    delete_run,
    execute_run,
    get_execution_history,
    get_run,
    get_run_audit,
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

# JS expression: true when the row has an in-flight (not yet persisted) op.
_IN_FLIGHT_JS = "['creating', 'running', 'reverting'].includes(props.row.status)"


def _can_execute_js(status_var: str) -> str:
    """JS expression: True when the run may be executed (no script on generation failure)."""
    return f"{status_var} !== 'generation_failed'"


def _mark_running(run_id: str, label: str) -> None:
    """Show a run's status as in-flight (spinner + label) in the table."""
    _running_runs[run_id] = label
    runs_table.refresh()


def _unmark_running(run_id: str) -> None:
    """Clear a run's in-flight status; the persisted status takes over."""
    _running_runs.pop(run_id, None)


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


def _broadcast_refresh() -> None:
    """Refresh the runs table on every connected client (safe from background tasks)."""
    for client in app.clients():
        with client:
            runs_table.refresh()


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
    }


def _columns() -> list[dict[str, Any]]:
    return [
        {"name": "name", "label": "Run", "field": "name", "align": "left", "sortable": True},
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


@ui.refreshable
def runs_table() -> None:
    """Render the runs table (refreshable so the timer / actions can rebuild it)."""
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

    table = ui.table(rows=rows, columns=_columns(), row_key="id", pagination={"rowsPerPage": 0})

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

    table.add_slot(
        "body-cell-actions",
        f"""
        <q-td :props="props">
            <q-btn dense flat icon="play_arrow" color="primary"
                   :disable="{_IN_FLIGHT_JS} || !{_can_execute_js("props.row.status")}"
                   @click="$parent.$emit('execute', props.row)" />
            <q-btn dense flat icon="undo" color="warning"
                   :disable="{_IN_FLIGHT_JS} || !{_can_revert_js("props.row.status")}"
                   @click="$parent.$emit('rollback', props.row)" />
            <q-btn dense flat icon="more_vert" color="grey-8"
                   :disable="{_IN_FLIGHT_JS}"
                   @click="$parent.$emit('menu', props.row)" />
            <q-btn dense flat icon="history" color="secondary"
                   :disable="{_IN_FLIGHT_JS}"
                   @click="$parent.$emit('history', props.row)" />
            <q-btn v-if="props.row.error" dense flat icon="warning" color="negative"
                   @click="$parent.$emit('errors', props.row)" />
        </q-td>
        """,
    )
    table.add_slot("no-data", '<div class="text-body1 text-grey-7 q-pa-md">No tasks</div>')

    def _on_execute(e: Any) -> None:
        run_id = e.args["name"] if isinstance(e.args, dict) else e.args
        _mark_running(run_id, "running")
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

    def _on_rename(e: Any) -> None:
        run_id = e.args["name"] if isinstance(e.args, dict) else e.args
        _rename_dialog(run_id)

    def _on_delete(e: Any) -> None:
        run_id = e.args["name"] if isinstance(e.args, dict) else e.args
        _delete_dialog(run_id)

    def _on_menu(e: Any) -> None:
        run_id = e.args["name"] if isinstance(e.args, dict) else e.args
        _actions_dialog(run_id)

    def _on_history(e: Any) -> None:
        run_id = e.args["name"] if isinstance(e.args, dict) else e.args
        _history_dialog(run_id)

    def _on_errors(e: Any) -> None:
        run_id = e.args["name"] if isinstance(e.args, dict) else e.args
        _error_dialog(run_id)

    table.on("execute", _on_execute)
    table.on("rollback", _on_rollback)
    table.on("rename", _on_rename)
    table.on("menu", _on_menu)
    table.on("history", _on_history)
    table.on("errors", _on_errors)
    table.on("delete", _on_delete)


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
        _broadcast_refresh()


def _confirm_dialog(
    title: str,
    message: str,
    on_confirm: Any,
    success_msg: str,
    run_id: str | None = None,
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
                    on_click=lambda: _confirm_and_close(dialog, on_confirm, success_msg, run_id),
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


def _actions_dialog(run_id: str) -> None:
    """Task modal: the run's prompt plus Rename / Delete actions.

    Created on the page layout (not inside the refreshable table) so the 5s
    auto-refresh doesn't destroy it — the previous inline ``q-btn-dropdown``
    closed whenever the table rebuilt.
    """
    try:
        detail = get_run(run_id)
    except Exception as exc:  # noqa: BLE001
        ui.notify(f"Failed to load run: {exc}", type="negative", multi_line=True)
        return

    def _rename() -> None:
        dialog.close()
        _rename_dialog(run_id)

    def _delete() -> None:
        dialog.close()
        _delete_dialog(run_id)

    def _view_error() -> None:
        dialog.close()
        _error_dialog(run_id)

    def _retry_generation() -> None:
        dialog.close()
        delete_run(run_id)
        _start_generation(run_id, detail.prompt, app.storage.user["user"], app.storage.user["password"])

    with context.client.layout:
        with ui.dialog() as dialog, ui.card().classes("w-full max-w-2xl"):
            ui.label(f"Task — {run_id}").classes("text-h6")
            ui.label(detail.prompt).classes("text-body1 q-mt-md")
            if detail.error:
                with ui.card().classes("w-full q-mt-md bg-red-1"):
                    first_line = detail.error.strip().splitlines()[0]
                    ui.label(f"Last error: {first_line}").classes("text-body2 text-red-10")
                    ui.button("View details", icon="warning", color="negative", on_click=_view_error).props("flat dense")
            if detail.status.value == "generation_failed":
                with ui.row().classes("w-full q-mt-md justify-end"):
                    ui.button("Retry generation", icon="refresh", color="primary", on_click=_retry_generation)
            with ui.row().classes("w-full q-mt-lg justify-end"):
                ui.button("Delete", icon="delete", color="negative", on_click=_delete)
                ui.button("Rename", icon="edit", on_click=_rename)
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
    """
    try:
        detail = get_run(run_id)
        records = get_run_audit(run_id)
    except Exception as exc:  # noqa: BLE001
        ui.notify(f"Failed to load run: {exc}", type="negative", multi_line=True)
        return

    with context.client.layout:
        with ui.dialog() as dialog, ui.card().classes("w-full max-w-3xl"):
            ui.label(f"Error details — {run_id}").classes("text-h6")
            if detail.error:
                ui.textarea(value=detail.error).classes("w-full font-mono text-caption").props(
                    "readonly outlined autogrow"
                ).style("max-height: 40vh")
            else:
                ui.label("No error recorded on this run.").classes("text-grey-7")

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
                audit_table = ui.table(
                    rows=rows,
                    columns=columns,
                    row_key="time",
                    pagination={"rowsPerPage": 0},
                ).classes("w-full")
                audit_table.add_slot(
                    "body-cell-outcome",
                    """
                    <q-td :props="props"
                          :class="props.row.outcome === 'failure' ? 'text-red-8' : ''">
                        {{ props.row.outcome }}
                    </q-td>
                    """,
                )
            with ui.row().classes("w-full q-mt-md justify-end"):
                ui.button("Close", on_click=dialog.close).props("color=grey-7 flat")
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
    dialog.close()

    async def _do() -> None:
        rename_run(old_id, new_id)

    background_tasks.create(_run_async(_do(), f"Renamed {old_id} → {new_id}"), name=f"rename {old_id}")


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


# Markdown shown in the "Capabilities" modal: the task types this service can
# carry out, each with concrete example prompts. Kept as a literal so the
# examples stay in sync with the orchestrator's documented capabilities.
_CAPABILITIES_MARKDOWN = """
This admin agent orchestrates a Uwazi instance. Describe a task in plain
language with **New Task**; the agent inspects the instance, then performs the
change through a generated, revertible migration **run**.

### Read-only / reporting
- "List all templates and how many entities each has"
- "Find entities whose title contains 'Annual Report 2024'"
- "Show the Countries thesaurus and how often each value is used"

### Schema — templates, thesauri, relationship types
- "Add a `summary` markdown field to the Document template"
- "Create a thesaurus `Topics` with values Human Rights, Environment, Health"
- "Rename the relationship type `authored_by` to `author`"

### Entities — create / update / delete (small batches, up to 5)
- "Create 3 new Person entities from this spreadsheet"
- "Set the `status` of entity `abc123` to `published`"

### Bulk entity operations (large sets, via the Python agent)
- "Publish all 2,000 entities in the Films template"
- "Delete every draft entity created before 2023"

### Relationships
- "Link entity A to entity B as `author`"

### Pages
- "Create a page showing a timeline of all books by date added"
- "Add a public page that lists every Country entity"

### Safety
Destructive operations (delete, publish/unpublish, schema removal) always require
an explicit confirmation before they run. Every run can be **reverted** from the
table, which restores backups and removes created entities.
"""


def _capabilities_dialog() -> None:
    """Show the service's supported task types with concrete examples.

    Created on the page layout (not inside the refreshable table) so the 5s
    auto-refresh doesn't destroy it. Mirrors ``_logs_dialog``'s modal layout.
    """
    with context.client.layout:
        with ui.dialog() as dialog, ui.card().classes("w-full"):
            dialog.props("maximized")
            with ui.row().classes("items-center justify-between q-mb-md"):
                ui.label("Service Capabilities").classes("text-h6")
                ui.button("Close", icon="close", on_click=lambda: dialog.close()).props("flat dense")
            content = ui.column().classes("w-full q-pr-md").style("height: calc(100vh - 120px); overflow-y: auto")
            with content:
                ui.markdown(_CAPABILITIES_MARKDOWN)
        dialog.open()


def _confirm_and_close(dialog: Any, on_confirm: Any, success_msg: str, run_id: str | None = None) -> None:
    dialog.close()
    # Let the outbox flush the close before the background task starts: the
    # revert/execute run a synchronous Uwazi login first, and blocking the
    # event loop before the flush stalls the modal visibly open.
    ui.timer(0.05, lambda: _start_confirmed_task(on_confirm, success_msg, run_id), once=True)


def _start_confirmed_task(on_confirm: Any, success_msg: str, run_id: str | None = None) -> None:
    result = on_confirm()
    if hasattr(result, "__await__"):
        if run_id is not None:
            _mark_running(run_id, "reverting")
        background_tasks.create(_run_async(result, success_msg, run_id), name=success_msg)
    else:
        ui.notify(success_msg, type="positive")
        runs_table.refresh()


def _new_task_wizard() -> None:
    """Multi-step dialog to create + generate a run."""
    state: dict[str, str] = {"name": "", "prompt": ""}

    with ui.dialog() as dialog, ui.card().classes("w-full max-w-4xl"):
        dialog.props("persistent")
        with ui.stepper() as stepper:
            _wizard_step_name(state, stepper, dialog)
            _wizard_step_prompt(state, stepper, dialog)
            _wizard_step_generate(state, dialog, stepper)
        dialog.open()


def _wizard_step_name(state: dict[str, str], stepper: Any, dialog: Any) -> None:
    with ui.step(name="name", title="Name", icon="edit"):
        ui.label("Name the run (a new folder will be created under data/runs/).").classes("text-body1 q-mb-md")
        name_input = ui.input(
            "Run name",
            placeholder="e.g. merge-entities-2026",
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


def _wizard_step_prompt(state: dict[str, str], stepper: Any, dialog: Any) -> None:
    with ui.step(name="prompt", title="Prompt", icon="chat"):
        ui.label("Describe the migration in natural language.").classes("text-body1 q-mb-md")
        prompt_input = (
            ui.textarea(
                "Prompt",
                placeholder="e.g. Merge duplicate entities sharing the title 'X'...",
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


def _wizard_prompt_next(state: dict[str, str], prompt_input: Any, stepper: Any) -> None:
    value = (prompt_input.value or "").strip()
    if not value:
        ui.notify("Prompt is required", type="warning")
        return
    state["prompt"] = value
    stepper.set_value("generate")


def _wizard_step_generate(state: dict[str, str], dialog: Any, stepper: Any) -> None:
    with ui.step(name="generate", title="Generate", icon="auto_awesome"):
        ui.label("Review and generate the migration script.").classes("text-body1 q-mb-md")
        ui.label().bind_text_from(state, "name", backward=lambda v: f"Run: {v}").classes("text-h6")
        ui.label().bind_text_from(
            state,
            "prompt",
            backward=lambda v: f"Prompt: {v[:120]}{'...' if len(v) > 120 else ''}",
        ).classes("text-body1")
        with ui.row().classes("q-mt-lg w-full justify-end"):
            ui.button("Cancel", on_click=lambda: dialog.close()).props("color=grey-7 flat")
            ui.button("Back", on_click=lambda: stepper.set_value("prompt")).props("color=grey-7 flat")
            ui.button(
                "Generate",
                icon="auto_awesome",
                on_click=lambda: _wizard_generate(state, dialog),
            )


def _start_generation(name: str, prompt: str, user: str, password: str) -> None:
    """Register the in-flight placeholder + notification and launch generation.

    Shared by the new-task wizard and the retry path on a ``generation_failed``
    run so both produce the identical toast/table/background-task flow.
    """
    _creating_runs[name] = {"name": name, "prompt": prompt}
    runs_table.refresh()
    _generating_notifications[name] = ui.notification(
        "Generating script (this may take a minute)...",
        type="ongoing",
        spinner=True,
        timeout=None,
    )
    background_tasks.create(
        _do_generate(name, prompt, user, password),
        name=f"generate {name}",
    )


def _wizard_generate(state: dict[str, str], dialog: Any) -> None:
    name = state.get("name", "")
    prompt = state.get("prompt", "")
    if not name or not prompt:
        ui.notify("Name and prompt are required", type="warning")
        return
    dialog.close()
    _start_generation(name, prompt, app.storage.user["user"], app.storage.user["password"])


async def _do_generate(name: str, prompt: str, user: str, password: str) -> None:
    # create_and_generate does synchronous Uwazi HTTP + LLM calls; run it on a
    # worker thread so the event loop stays free to serve the UI and websocket.
    try:
        await asyncio.to_thread(asyncio.run, create_and_generate(name, prompt, user, password))
        _broadcast_notify(f"Run {name!r} created and generated", type="positive")
    except Exception as exc:  # noqa: BLE001
        _notify_error("Generation failed", str(exc))
    finally:
        _creating_runs.pop(name, None)
        notification = _generating_notifications.pop(name, None)
        if notification is not None:
            notification.dismiss()
        _broadcast_refresh()


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
            ui.separator().props("vertical").classes("q-mx-sm")
            ui.button("Capabilities", icon="info", on_click=_capabilities_dialog).props("color=secondary flat")
            ui.button("Logs", icon="terminal", on_click=_logs_dialog).props("color=secondary flat")
            ui.button("New Task", icon="add", on_click=_new_task_wizard)
            ui.button("Log out", icon="lock", on_click=_logout).props("color=secondary flat")

    with ui.column().classes("w-full items-center"):
        with ui.card().classes("w-full max-w-6xl"):
            ui.label("Tasks").classes("text-h5")
            runs_table()

    ui.timer(5.0, runs_table.refresh)

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
