"""Multistep task use case: create, list, revert the whole task.

The whole feature rests on one mapping — **one step = one run** — which is what
lets every existing mechanism apply unchanged: a step's run is generated,
scanned as the planning pass (its recorded writes are the review table), and
reverted by the machinery that already exists.

One invariant is enforced here rather than in the UI (the UI is a driver; the
rules belong to the use case):

- **Whole-task revert only**: there is no per-step revert. ``revert_task`` walks
  ``task_revert_plan`` in order (later steps first, apply run before its step
  run), stopping on the first failure and leaving the remaining plan visible for
  retry — a partially reverted task is a safe, inspectable state.

Previews are dry runs: they read real entities and record what the script WOULD
write, so the step cards can show implication counts before anything runs.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from loguru import logger

from uwazi_admin_agent.domain.manifest import RunStatus
from uwazi_admin_agent.domain.task import (
    StepDef,
    StepOrderError,
    TaskDef,
    TaskView,
    build_task_view,
    ordered,
    task_revert_plan,
)
from uwazi_admin_agent.ports.backup_store_port import BackupStorePort
from uwazi_admin_agent.ports.task_store_port import TaskStorePort


class TaskUseCase:
    """Orchestration over the existing per-run machinery."""

    def __init__(
        self,
        task_store: TaskStorePort,
        backup_store: BackupStorePort,
        revert_run: object,
        create_run: object,
    ) -> None:
        """``revert_run``/``create_run`` are async callables.

        Injected as callables (not use cases) because this class is pure
        orchestration: it sequences calls and applies the state gates, leaving the
        actual work to the same functions the single-run flow uses. Their
        signatures are the single-run ones —
        ``revert_run(run_id, user, password)`` and
        ``create_run(run_id, prompt, user, password)``.
        """
        self._tasks: TaskStorePort = task_store
        self._backup: BackupStorePort = backup_store
        self._revert_run: Callable[..., Awaitable[None]] = revert_run
        self._create_run: Callable[..., Awaitable[None]] = create_run

    # --- creation ------------------------------------------------------------

    async def create_task(
        self,
        task_id: str,
        name: str,
        steps: list[tuple[str, str, str]],
        *,
        user: str,
        password: str,
    ) -> TaskDef:
        """Create a run per ``(run_id, title, prompt)`` step and persist the task.

        Runs are created sequentially so each step's generation is approved on its
        own (the caller opens a cost dialog per step) and so a failure part-way
        leaves a task whose already-created steps are reviewable rather than
        everything lost. ``create_run`` is called with the signature the single-run
        flow already has: ``(run_id, prompt, user, password)``.
        """
        step_defs: list[StepDef] = []
        for index, (run_id, title, prompt) in enumerate(steps):
            step_defs.append(StepDef(step_no=index + 1, title=title, description=prompt, run_id=run_id))
            await self._create_run(run_id, prompt, user, password)
        task = TaskDef(
            task_id=task_id,
            name=name,
            steps=step_defs,
            created_at=datetime.now(timezone.utc),
        )
        self._tasks.save_task(task)
        logger.info("task created id={} steps={}", task_id, len(step_defs))
        return task

    def add_existing_run(self, task_id: str, run_id: str, title: str, description: str = "") -> TaskDef:
        """Link an already-created run into a task as its next step."""
        task = self._tasks.load_task(task_id)
        step = StepDef(step_no=len(task.steps) + 1, title=title, description=description, run_id=run_id)
        return self._tasks.add_step(task_id, step)

    # --- reading -------------------------------------------------------------

    def _statuses(self, task: TaskDef) -> dict[str, RunStatus]:
        """Read every step run's status from its manifest.

        A reviewed Run writes into the step's OWN manifest, so there is no
        second run to look up: the run's status already reflects it.
        """
        step_statuses: dict[str, RunStatus] = {}
        for step in ordered(task.steps):
            try:
                step_statuses[step.run_id] = self._backup.load_manifest(step.run_id).status
            except FileNotFoundError:
                continue
        return step_statuses

    def get_task(self, task_id: str, in_flight: set[str] | None = None) -> TaskView:
        """Return a task's live view (steps + statuses + the revert plan)."""
        task = self._tasks.load_task(task_id)
        step_statuses = self._statuses(task)
        view = build_task_view(task, step_statuses)
        if in_flight:
            # Re-derive the plan with the in-flight guard applied, so the UI shows
            # an accurate (empty/raising) plan rather than one that cannot run.
            plan = task_revert_plan(task.steps, step_statuses, in_flight)
            return view.model_copy(update={"revert_targets": plan})
        return view

    def list_tasks(self) -> list[TaskDef]:
        """Every task definition (newest first by creation time)."""
        return sorted(self._tasks.list_tasks(), key=lambda t: t.created_at, reverse=True)

    def delete_task(self, task_id: str) -> None:
        """Remove a task's definition (its runs are removed by the caller)."""
        self._tasks.delete_task(task_id)

    # --- whole-task revert ---------------------------------------------------

    async def revert_task(self, task_id: str, user: str, password: str, in_flight: set[str] | None = None) -> list[str]:
        """Revert EVERY executed step, in reverse order (the only revert entry).

        Returns the run ids actually reverted. Stops at the first failure (raising
        the underlying error) so the operator sees exactly where the sequence
        stopped and can retry the remainder.
        """
        task = self._tasks.load_task(task_id)
        step_statuses = self._statuses(task)
        plan = task_revert_plan(task.steps, step_statuses, in_flight)
        if not plan:
            raise StepOrderError("nothing to revert: no step run has live writes")
        reverted: list[str] = []
        for kind, run_id in plan:
            logger.info("task revert {} step-target {} ({})", task_id, run_id, kind)
            await self._revert_run(run_id, user, password)
            reverted.append(run_id)
        return reverted


def _step_at(task: TaskDef, step_no: int) -> StepDef:
    """The step with ``step_no`` (raises ``StepOrderError`` when absent)."""
    for step in ordered(task.steps):
        if step.step_no == step_no:
            return step
    raise StepOrderError(f"task {task.task_id!r} has no step {step_no}")


__all__ = ["TaskUseCase"]
