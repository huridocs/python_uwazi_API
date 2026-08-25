"""NiceGUI web UI for the admin agent — a driver replacing the CLI for common ops.

Single page: a table of runs with status badges + row actions (execute, rollback,
delete), and a "New Task" wizard (stepper) to create + generate a run. Mutating
operations run as background tasks; the table auto-refreshes every 5s.

This is a driver: it wires the service layer (:mod:`run_service`) to the UI and
contains no business logic, matching the ``drivers/`` layer convention.
"""

from __future__ import annotations

import os
from typing import Any

from nicegui import app, background_tasks, context, ui

from uwazi_admin_agent.drivers.web.run_service import (
    RunSummary,
    create_and_generate,
    delete_run,
    execute_run,
    get_run,
    list_runs,
    revert_run,
)

# Quasar color names for each status value (used by the status badge slot).
# "creating" is a UI-only status (not a persisted RunStatus) for runs whose
# generation is in flight — the manifest is only saved after generation completes.
_STATUS_COLORS: dict[str, str] = {
    "creating": "blue-grey",
    "planned": "grey",
    "snapshotted": "orange",
    "executed": "green",
    "verified": "blue",
    "reverted": "indigo",
    "failed": "red",
}

# Transient UI state: runs whose script is being generated (not yet persisted).
# The manifest is only saved after generation completes, so "creating" is a
# UI-only status tracked here — not a persisted RunStatus value.
_creating_runs: dict[str, dict[str, Any]] = {}

# JS set-literal string injected into the status badge slot for color lookup.
_STATUS_COLOR_JS = "{" + ", ".join(f"'{k}': '{v}'" for k, v in _STATUS_COLORS.items()) + "}"


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


def _summary_to_row(run: RunSummary) -> dict[str, Any]:
    return {
        "id": run.run_id,
        "name": run.run_id,
        "status": run.status.value,
        "created": run.created_at.strftime("%Y-%m-%d %H:%M"),
        "modified": run.modified,
        "deleted": run.deleted,
        "created_count": run.created,
        "rewired": run.rewired,
    }


def _creating_to_row(name: str) -> dict[str, Any]:
    """A placeholder row for a run whose generation is in flight."""
    return {
        "id": name,
        "name": name,
        "status": "creating",
        "created": "—",
        "modified": 0,
        "deleted": 0,
        "created_count": 0,
        "rewired": 0,
    }


def _columns() -> list[dict[str, Any]]:
    return [
        {"name": "name", "label": "Run", "field": "name", "align": "left", "sortable": True},
        {"name": "status", "label": "Status", "field": "status", "align": "left", "sortable": True},
        {"name": "created", "label": "Created", "field": "created", "align": "left", "sortable": True},
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

    table = ui.table(rows=rows, columns=_columns(), row_key="id", pagination={"rowsPerPage": 0})

    table.add_slot(
        "body-cell-status",
        f"""
        <q-td :props="props">
            <q-badge :color="({_STATUS_COLOR_JS})[props.row.status] || 'grey'"
                     :label="props.row.status" />
        </q-td>
        """,
    )

    table.add_slot(
        "body-cell-actions",
        f"""
        <q-td :props="props">
            <q-btn dense flat icon="play_arrow" color="primary"
                   :disable="['creating','executed','reverted'].includes(props.row.status)"
                   @click="$parent.$emit('execute', props.row)" />
            <q-btn dense flat icon="undo" color="warning"
                   :disable="props.row.status === 'creating' || !{_can_revert_js("props.row.status")}"
                   @click="$parent.$emit('rollback', props.row)" />
            <q-btn dense flat icon="description" color="info"
                   :disable="props.row.status === 'creating'"
                   @click="$parent.$emit('viewprompt', props.row)" />
            <q-btn dense flat icon="delete" color="red"
                   :disable="props.row.status === 'creating'"
                   @click="$parent.$emit('delete', props.row)" />
        </q-td>
        """,
    )
    table.add_slot("no-data", '<div class="text-body1 text-grey-7 q-pa-md">No tasks</div>')

    def _on_execute(e: Any) -> None:
        run_id = e.args["name"] if isinstance(e.args, dict) else e.args
        background_tasks.create(_run_async(execute_run(run_id), f"Executed {run_id}"), name=f"execute {run_id}")

    def _on_rollback(e: Any) -> None:
        run_id = e.args["name"] if isinstance(e.args, dict) else e.args
        _confirm_dialog(
            "Rollback run",
            f"Revert run {run_id!r}? This restores every backed-up entity and deletes created ones.",
            lambda: revert_run(run_id),
            success_msg=f"Reverted {run_id}",
        )

    def _on_delete(e: Any) -> None:
        run_id = e.args["name"] if isinstance(e.args, dict) else e.args
        _confirm_dialog(
            "Delete run",
            f"Permanently remove run {run_id!r}? This deletes the entire run folder. "
            "If the run is EXECUTED, revert will no longer be possible.",
            lambda: delete_run(run_id),
            success_msg=f"Deleted {run_id}",
        )

    def _on_view_prompt(e: Any) -> None:
        run_id = e.args["name"] if isinstance(e.args, dict) else e.args
        _prompt_dialog(run_id)

    table.on("execute", _on_execute)
    table.on("rollback", _on_rollback)
    table.on("viewprompt", _on_view_prompt)
    table.on("delete", _on_delete)


async def _run_async(coro: Any, success_msg: str) -> None:
    """Await a coroutine, notify on success/error, then refresh the table."""
    try:
        await coro
        _broadcast_notify(success_msg, type="positive")
    except Exception as exc:  # noqa: BLE001 — surface every failure to the operator
        _broadcast_notify(f"Error: {exc}", type="negative", multi_line=True)
    finally:
        _broadcast_refresh()


def _confirm_dialog(
    title: str,
    message: str,
    on_confirm: Any,
    success_msg: str,
) -> None:
    """Yes/no confirmation dialog; runs ``on_confirm`` (sync or async) on confirm.

    Created on the page layout (not inside the refreshable table) so the 5s
    auto-refresh doesn't destroy it.
    """
    with context.client.layout:
        with ui.dialog() as dialog, ui.card():
            ui.label(title).classes("text-h6")
            ui.label(message).classes("text-body1")
            with ui.row():
                ui.button("Cancel", on_click=lambda: dialog.close())
                ui.button(
                    "Confirm",
                    color="negative",
                    on_click=lambda: _confirm_and_close(dialog, on_confirm, success_msg),
                )
    dialog.open()


def _prompt_dialog(run_id: str) -> None:
    """Show the run's prompt in a read-only modal.

    Created on the page layout (not inside the refreshable table) so the 5s
    auto-refresh doesn't destroy it.
    """
    try:
        detail = get_run(run_id)
    except Exception as exc:  # noqa: BLE001
        ui.notify(f"Failed to load run: {exc}", type="negative", multi_line=True)
        return
    with context.client.layout:
        with ui.dialog() as dialog, ui.card().classes("w-full max-w-2xl"):
            ui.label(f"Prompt — {run_id}").classes("text-h6")
            ui.label(detail.prompt).classes("text-body1 q-mt-md")
            with ui.row().classes("q-mt-lg"):
                ui.button("Close", on_click=lambda: dialog.close())
        dialog.open()
        dialog.props("persistent")


def _confirm_and_close(dialog: Any, on_confirm: Any, success_msg: str) -> None:
    dialog.close()
    result = on_confirm()
    if hasattr(result, "__await__"):
        background_tasks.create(_run_async(result, success_msg), name=success_msg)
    else:
        ui.notify(success_msg, type="positive")
        runs_table.refresh()


def _new_task_wizard() -> None:
    """Multi-step dialog to create + generate a run."""
    state: dict[str, str] = {"name": "", "prompt": ""}

    with ui.dialog() as dialog:
        dialog.props("maximized")
        with ui.card().classes("w-full"):
            with ui.stepper() as stepper:
                _wizard_step_name(state, stepper)
                _wizard_step_prompt(state, stepper)
                _wizard_step_generate(state, dialog, stepper)
        dialog.open()


def _wizard_step_name(state: dict[str, str], stepper: Any) -> None:
    with ui.step(name="name", title="Name", icon="edit"):
        ui.label("Name the run (a new folder will be created under data/runs/).").classes("text-body1 q-mb-md")
        name_input = ui.input(
            "Run name",
            placeholder="e.g. merge-entities-2026",
            validation={"Required": lambda v: bool(v and v.strip())},
        ).classes("w-full text-h6")
        with ui.row().classes("q-mt-lg"):
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


def _wizard_step_prompt(state: dict[str, str], stepper: Any) -> None:
    with ui.step(name="prompt", title="Prompt", icon="chat"):
        ui.label("Describe the migration in natural language.").classes("text-body1 q-mb-md")
        prompt_input = (
            ui.textarea(
                "Prompt",
                placeholder="e.g. Merge duplicate entities sharing the title 'X'...",
                validation={"Required": lambda v: bool(v and v.strip())},
            )
            .classes("w-full")
            .props("autogrow")
        )
        with ui.row().classes("q-mt-lg"):
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
        with ui.row().classes("q-mt-lg"):
            ui.button("Back", on_click=lambda: stepper.set_value("prompt")).props("color=grey-7 flat")
            ui.button(
                "Generate",
                icon="auto_awesome",
                on_click=lambda: _wizard_generate(state, dialog),
            )


def _wizard_generate(state: dict[str, str], dialog: Any) -> None:
    name = state.get("name", "")
    prompt = state.get("prompt", "")
    if not name or not prompt:
        ui.notify("Name and prompt are required", type="warning")
        return
    dialog.close()
    _creating_runs[name] = {"name": name, "prompt": prompt}
    runs_table.refresh()
    ui.notify("Generating script (this may take a minute)...", type="ongoing")
    background_tasks.create(_do_generate(name, prompt), name=f"generate {name}")


async def _do_generate(name: str, prompt: str) -> None:
    try:
        await create_and_generate(name, prompt)
        _broadcast_notify(f"Run {name!r} created and generated", type="positive")
    except Exception as exc:  # noqa: BLE001
        _broadcast_notify(f"Generate failed: {exc}", type="negative", multi_line=True)
    finally:
        _creating_runs.pop(name, None)
        _broadcast_refresh()


def _build_page() -> None:
    """Build the single-page layout."""
    ui.colors(primary="#2c3e50", secondary="#18bc9c", accent="#f39c12")
    with ui.header().classes("items-center justify-between"):
        ui.label("Uwazi Admin Agent").classes("text-h6")
        ui.button("New Task", icon="add", on_click=_new_task_wizard)

    with ui.column().classes("w-full items-center"):
        with ui.card().classes("w-full max-w-6xl"):
            ui.label("Migration Runs").classes("text-h5")
            runs_table()

    ui.timer(5.0, runs_table.refresh)


@ui.page("/")
def _index() -> None:
    _build_page()


def main() -> None:
    """Run the NiceGUI server (uvicorn under the hood) on port 5055."""
    port = int(os.environ.get("ADMIN_WEB_PORT", "5055"))
    ui.run(host="0.0.0.0", port=port, reload=False, title="Uwazi Admin Agent")


if __name__ == "__main__":
    main()
