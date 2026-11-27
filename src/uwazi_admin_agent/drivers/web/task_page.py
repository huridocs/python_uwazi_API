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
from nicegui import background_tasks, context, ui

from uwazi_admin_agent.domain.task import StepView, TaskView
from uwazi_admin_agent.drivers.web.instances import page_link
from uwazi_admin_agent.drivers.web.operation_gate import (
    busy_text,
    release,
    release_runs,
    run_claim,
    run_claim_text,
    snapshot,
    try_acquire,
    try_claim_runs,
)
from uwazi_admin_agent.drivers.web.run_service import (
    revert_run,
    task_view,
)
from uwazi_admin_agent.drivers.web.session import (
    bind_client_instance,
    credentials_for_instance,
    page_instance_url,
    session_is_active,
)

from .header import instance_badge
from .theme import install_theme

_STEP_COLORS: dict[str, str] = {
    "planned": "grey",
    "executed": "green",
    "verified": "blue",
    "reverted": "indigo",
    "failed": "red",
    "generation_failed": "deep-orange",
    "snapshotted": "orange",
}


def task_page(task_id: str, instance_url: str) -> None:
    """Render the task page (login required).

    ``instance_url`` is the configured instance the path prefix named; a
    session logged in to another instance (or none) is sent to that instance's
    login page.
    """
    if not session_is_active(instance_url):
        ui.navigate.to(page_link("/", instance_url))
        return
    install_theme()
    _build_page(task_id)


def _build_page(task_id: str) -> None:
    bind_client_instance()
    instance = page_instance_url()
    try:
        view = task_view(instance, task_id)
    except Exception as exc:  # noqa: BLE001 — a missing task shows an error, never a crash
        with ui.column().classes("w-full items-center justify-center min-h-screen"):
            ui.label(f"Cannot open task {task_id!r}: {exc}").classes("text-body1 text-negative")
            ui.button("Back to tasks", on_click=lambda: ui.navigate.to(page_link("/app"))).props("flat")
        return

    state: dict[str, Any] = {"task_id": task_id, "instance_url": instance, "view": view, "cards": {}}

    with ui.header().classes("uw-header items-center justify-between"):
        with ui.row().classes("items-center gap-3 no-wrap"):
            ui.button(icon="arrow_back", on_click=lambda: ui.navigate.to(page_link("/app"))).props("flat round").classes(
                "uw-btn-ghost"
            )
            with ui.element("div").classes("brand-mark"):
                ui.label("u")
            with ui.column().classes("gap-0"):
                ui.label("Uwazi Admin Agent").classes("brand-name")
                ui.label("Task").classes("brand-sub")
            ui.element("div").classes("header-separator")
            with ui.row().classes("items-center gap-1 no-wrap"):
                ui.link("Tasks", page_link("/app")).classes("breadcrumb-link")
                ui.icon("chevron_right").classes("breadcrumb-chevron")
                ui.label(view.name).classes("breadcrumb-current")
                ui.badge(view.status).props(f"color={_task_color(view.status)}").classes("q-ml-sm status-pill")
        with ui.row().classes("items-center gap-2 no-wrap"):
            instance_badge(instance)
            ui.button("Revert task", icon="undo", on_click=lambda: _revert_task(state)).props("outline no-caps").classes(
                "uw-btn-danger"
            )
            ui.button("Back to tasks", on_click=lambda: ui.navigate.to(page_link("/app"))).props("flat no-caps").classes(
                "uw-btn-ghost"
            )

    with ui.column().classes("w-full items-center"):
        with ui.element("div").classes("uw-page w-full"):
            with ui.element("div").classes("uw-card w-full"):
                with ui.row().classes("uw-card-header w-full items-center justify-between no-wrap"):
                    with ui.column().classes("gap-0"):
                        ui.label("Task steps").classes("uw-card-title")
                        ui.label(
                            "Steps run in order. Running rows in a step's “Review & apply” page is what writes, "
                            "straight into that step's run; “Revert task” undoes every executed step, latest first."
                        ).classes("uw-card-subtitle")
                with ui.column().classes("uw-card-body w-full gap-3"):
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
    with ui.card().classes("w-full uw-step-card"):
        with ui.row().classes("w-full items-center justify-between no-wrap"):
            with ui.row().classes("items-center gap-2 no-wrap"):
                ui.label(f"{step.step_no}. {step.title}").classes("uw-step-title")
                status = step.status.value if step.status is not None else "missing"
                ui.badge(status).props(f"color={_STEP_COLORS.get(status, 'grey')}").classes("status-pill")
            with ui.row().classes("items-center no-wrap gap-1"):
                ui.button(
                    "Review",
                    icon="fact_check",
                    on_click=lambda s=step: ui.navigate.to(page_link(f"/review/{s.run_id}")),
                ).props("flat no-caps color=primary")
                ui.button(
                    "Show run",
                    icon="open_in_new",
                    on_click=lambda s=step: ui.navigate.to(page_link("/app")),
                ).props("flat no-caps").classes("uw-btn-ghost")
        if step.description:
            ui.label(step.description).classes("uw-step-description")


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
    instance = state["instance_url"]
    credentials = credentials_for_instance(instance)
    if credentials is None:
        ui.notify("Not logged in to Uwazi", type="negative")
        return
    view: TaskView = state["view"]
    run_ids = [run_id for _kind, run_id in view.revert_targets]
    # A whole-task revert writes Uwazi for several runs in sequence. It claims
    # every target run atomically and then THIS instance's single write slot:
    # another instance's pass is never a conflict, but a Run/Revert already in
    # flight on this instance must finish first.
    if not try_claim_runs(instance, run_ids, "revert"):
        busy = next((claim for claim in map(lambda rid: run_claim(instance, rid), run_ids) if claim), None)
        ui.notify(run_claim_text(busy), type="warning", multi_line=True)
        return
    if not try_acquire(instance, "revert", state["task_id"]):
        ui.notify(busy_text(snapshot(instance)), type="warning", multi_line=True)
        release_runs(instance, run_ids, "revert")
        return
    done: list[str] = []
    try:
        for kind, run_id in view.revert_targets:
            await revert_run(instance, run_id, credentials.user, credentials.password)
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
        release(instance, "revert", state["task_id"])
        release_runs(instance, run_ids, "revert")
        _reload(state)


def _reload(state: dict[str, Any]) -> None:
    """Re-read the task view and re-render the step cards."""
    if not session_is_active(state["instance_url"]):
        return
    try:
        state["view"] = task_view(state["instance_url"], state["task_id"])
        _render_steps(state)
    except Exception:  # noqa: BLE001 — a refresh failure must not break the page
        logger.exception("task refresh failed")


def _refresh(state: dict[str, Any]) -> None:
    _reload(state)


def _confirm(title: str, message: str, on_confirm: Any) -> None:
    with context.client.layout:
        with ui.dialog() as dialog, ui.card().classes("w-full max-w-2xl uw-dialog"):
            ui.label(title).classes("uw-modal-title")
            ui.label(message).classes("text-body2").style("white-space: pre-wrap")
            with ui.row().classes("uw-dialog-footer"):
                ui.button("Cancel", on_click=dialog.close).props("flat no-caps").classes("uw-btn-ghost")
                ui.button("Confirm", color="negative", on_click=lambda: _fire(dialog, on_confirm)).props(
                    "unelevated no-caps"
                )
    dialog.open()


def _fire(dialog: Any, on_confirm: Any) -> None:
    dialog.close()
    on_confirm()


__all__ = ["task_page"]
