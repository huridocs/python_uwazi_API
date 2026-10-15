"""The multistep task page: step cards, whole-task revert.

One page per task (``/task/{task_id}``). Each card shows a step's status, a
"Review" link (the review page lists the precomputed rows and their Run
buttons) and the step's run link — there is NO whole-set "Execute" button:
running rows in the review page is what writes. The header carries the derived task status and the SINGLE
"Revert task" button — reverting is whole-task by design (a reviewed Run writes
into its own step's manifest, so reverting that step undoes it), so there is no
per-step revert anywhere in this UI.
"""

from __future__ import annotations

from typing import Any

from loguru import logger
from nicegui import app, background_tasks, context, ui

from uwazi_admin_agent.domain.task import StepView, TaskView
from uwazi_admin_agent.drivers.web.run_service import (
    revert_run,
    task_view,
)

_STEP_COLORS: dict[str, str] = {
    "planned": "grey",
    "executed": "green",
    "verified": "blue",
    "reverted": "indigo",
    "failed": "red",
    "generation_failed": "deep-orange",
    "snapshotted": "orange",
}


@ui.page("/task/{task_id}")
def task_page(task_id: str) -> None:
    """Render the task page (login required)."""
    if not app.storage.user.get("user"):
        ui.navigate.to("/")
        return
    ui.colors(primary="#2c3e50", secondary="#18bc9c", accent="#f39c12")
    _build_page(task_id)


def _build_page(task_id: str) -> None:
    try:
        view = task_view(task_id)
    except Exception as exc:  # noqa: BLE001 — a missing task shows an error, never a crash
        with ui.column().classes("w-full items-center justify-center min-h-screen"):
            ui.label(f"Cannot open task {task_id!r}: {exc}").classes("text-body1 text-negative")
            ui.button("Back to tasks", on_click=lambda: ui.navigate.to("/app")).props("flat")
        return

    state: dict[str, Any] = {"task_id": task_id, "view": view, "cards": {}}

    with ui.header().classes("items-center justify-between"):
        with ui.row().classes("items-center"):
            ui.button(icon="arrow_back", on_click=lambda: ui.navigate.to("/app")).props("flat color=secondary")
            ui.label(view.name).classes("text-h6 q-ml-sm")
            ui.badge(view.status).props(f"color={_task_color(view.status)}").classes("q-ml-sm")
        with ui.row().classes("items-center"):
            ui.button("Revert task", icon="undo", on_click=lambda: _revert_task(state)).props("flat color=warning")
            ui.button("Back to tasks", on_click=lambda: ui.navigate.to("/app")).props("flat color=secondary")

    with ui.column().classes("w-full items-center"):
        ui.label(
            "Steps run in order. Running rows in a step's 'Review & apply' page is what writes, "
            "straight into that step's run; 'Revert task' undoes every executed step, latest first."
        ).classes("text-body2 text-grey-7 w-full max-w-6xl q-mb-sm")
        with ui.card().classes("w-full max-w-6xl"):
            _render_steps(state)

    ui.timer(5.0, lambda: _refresh(state))


def _task_color(status: str) -> str:
    return {
        "planned": "grey",
        "running": "blue",
        "partially_reverted": "orange",
        "reverted": "indigo",
        "failed": "red",
    }.get(status, "grey")


def _render_steps(state: dict[str, Any]) -> None:
    """Render one card per step into this client's step column."""
    container = state.get("_steps")
    if container is None:
        container = ui.column().classes("w-full")
        state["_steps"] = container
    container.clear()
    view: TaskView = state["view"]
    with container:
        for step in view.steps:
            _step_card(state, step, view)


def _step_card(state: dict[str, Any], step: StepView, view: TaskView) -> None:
    with ui.card().classes("w-full"):
        with ui.row().classes("w-full items-center justify-between no-wrap"):
            with ui.row().classes("items-center"):
                ui.label(f"{step.step_no}. {step.title}").classes("text-subtitle1 text-bold")
                status = step.status.value if step.status is not None else "missing"
                ui.badge(status).props(f"color={_STEP_COLORS.get(status, 'grey')}")
            with ui.row().classes("items-center no-wrap"):
                ui.button(
                    "Review",
                    icon="fact_check",
                    on_click=lambda s=step: ui.navigate.to(f"/review/{s.run_id}"),
                ).props("dense flat color=secondary")
                ui.button(
                    "Show run",
                    icon="open_in_new",
                    on_click=lambda s=step: ui.navigate.to("/app"),
                ).props("dense flat grey-8")
        if step.description:
            ui.label(step.description).classes("text-body2 text-grey-7")


# --- actions -----------------------------------------------------------------


def _revert_task(state: dict[str, Any]) -> None:
    """Revert the whole task: show the plan, then run it in order."""
    view: TaskView = state["view"]
    if not view.revert_targets:
        ui.notify("Nothing to revert: no step has live writes", type="warning")
        return
    plan_lines = "\n".join(f"{index + 1}. {kind} run {run_id}" for index, (kind, run_id) in enumerate(view.revert_targets))

    def _confirm_revert() -> None:
        background_tasks.create(_run_revert_task(state), name=f"revert-task {state['task_id']}")

    _confirm(
        "Revert the whole task",
        f"This will revert, in order (latest step first):\n\n{plan_lines}\n\n"
        "A failure stops the sequence; the remaining plan stays visible for retry.",
        _confirm_revert,
    )


async def _run_revert_task(state: dict[str, Any]) -> None:
    user = app.storage.user.get("user")
    password = app.storage.user.get("password")
    if not user or not password:
        ui.notify("Not logged in to Uwazi", type="negative")
        return
    view: TaskView = state["view"]
    done: list[str] = []
    try:
        for kind, run_id in view.revert_targets:
            await revert_run(run_id, user, password)
            done.append(f"{kind} {run_id}")
        ui.notify(f"Task reverted ({len(done)} run(s))", type="positive")
    except Exception as exc:  # noqa: BLE001 — leave the remainder visible for retry
        logger.exception("task revert failed")
        ui.notify(
            f"Revert stopped after {len(done)} run(s): {exc}",
            type="negative",
            multi_line=True,
        )
    finally:
        _reload(state)


def _reload(state: dict[str, Any]) -> None:
    """Re-read the task view and re-render the step cards."""
    try:
        state["view"] = task_view(state["task_id"])
        _render_steps(state)
    except Exception:  # noqa: BLE001 — a refresh failure must not break the page
        logger.exception("task refresh failed")


def _refresh(state: dict[str, Any]) -> None:
    _reload(state)


def _confirm(title: str, message: str, on_confirm: Any) -> None:
    with context.client.layout:
        with ui.dialog() as dialog, ui.card().classes("w-full max-w-2xl"):
            ui.label(title).classes("text-h6")
            ui.label(message).classes("text-body2").style("white-space: pre-wrap")
            with ui.row().classes("w-full justify-end"):
                ui.button("Cancel", on_click=dialog.close).props("color=grey-7 flat")
                ui.button("Confirm", color="negative", on_click=lambda: _fire(dialog, on_confirm))
    dialog.open()


def _fire(dialog: Any, on_confirm: Any) -> None:
    dialog.close()
    on_confirm()


__all__ = ["task_page"]
