"""Multistep tasks: the step model plus the pure gates over run statuses.

A **task** is an ordered list of steps; each step owns exactly one run
(``StepDef.run_id``), which is the unit that generates, previews, executes, and
reverts. Mapping one step to one run is what lets the whole multistep feature
reuse the existing run machinery untouched: the manifest, the backup intercept,
the execute/revert gates, and the review/prediction flow all keep operating on a
run.

Two invariants shape this module:

- **Forward order**: a step may execute only when every earlier step has been
  executed (``next_executable``), so a task never applies step 3 over a state
  step 2 was supposed to establish.
- **Whole-task revert only**: there is no per-step revert. ``task_revert_plan``
  produces the strictly reverse-ordered list of revert targets — later steps
  first, and within a step its *apply run* before the step's own run — so
  snapshots restore correctly even when two steps touched the same entities.

Everything here is a pure function of statuses, which is why it is the unit-test
target for the ordering rules.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from uwazi_admin_agent.domain.manifest import RunStatus

# Statuses that mean "this run's writes are live and revert can undo them".
_LIVE_STATUSES: frozenset[RunStatus] = frozenset({RunStatus.EXECUTED, RunStatus.VERIFIED, RunStatus.FAILED})

# In-flight operations are UI state in this codebase (the global op claim plus
# the "running"/"reverting" labels), not a persisted RunStatus — so the revert
# plan takes an explicit set of run ids instead of inferring one. SNAPSHOTTED is
# a pre-execution status here (snapshots happen inside execute), not a marker of
# a live pass.

TASK_PLANNED: str = "planned"
TASK_RUNNING: str = "running"
TASK_PARTIALLY_REVERTED: str = "partially_reverted"
TASK_REVERTED: str = "reverted"
TASK_FAILED: str = "failed"


class StepOrderError(RuntimeError):
    """Raised when a step or task operation would violate the ordering invariant."""


class StepDef(BaseModel):
    """One step of a task: its title, description, and the run that carries it."""

    model_config = ConfigDict(frozen=True)

    step_no: int = Field(ge=1, description="1-based position in the task's ordered step list.")
    title: str = Field(description="Short operator-facing step label.")
    description: str = Field(default="", description="What this step does (shown on the step card).")
    run_id: str = Field(description="The run this step owns: generate/preview/execute/revert all target it.")


class TaskDef(BaseModel):
    """A multistep task: an ordered list of steps plus its identity."""

    model_config = ConfigDict(frozen=True)

    task_id: str = Field(description="Unique id; also the YAML filename under TASKS_PATH.")
    name: str = Field(description="Operator-facing task name.")
    steps: list[StepDef] = Field(default_factory=list, description="Steps in execution order.")
    created_at: datetime = Field(description="When the task was created.")


class StepView(BaseModel):
    """A step's live view: its definition plus its run's current status."""

    model_config = ConfigDict(frozen=True)

    step_no: int
    title: str
    description: str
    run_id: str
    status: RunStatus | None = Field(default=None, description="The step run's status; None when the run is missing.")


class TaskView(BaseModel):
    """A task's live view: its steps, the next executable step, and the revert plan."""

    model_config = ConfigDict(frozen=True)

    task_id: str
    name: str
    created_at: datetime
    steps: list[StepView]
    status: str = Field(description="Derived task status (planned/running/partially_reverted/reverted/failed).")
    next_step: int | None = Field(default=None, description="The next step that may execute, if any.")
    revert_targets: list[tuple[str, str]] = Field(
        default_factory=list,
        description=(
            "The ordered (kind, run_id) list a whole-task revert would run. Only 'step' targets exist: "
            "a reviewed Run writes into its own run's manifest, so there is no separate run to revert."
        ),
    )


def ordered(steps: list[StepDef]) -> list[StepDef]:
    """Return the steps in ascending ``step_no`` order (pure; input untouched)."""
    return sorted(steps, key=lambda s: s.step_no)


def _status_of(statuses: dict[str, RunStatus], run_id: str) -> RunStatus:
    """Look up a run's status, defaulting to ``PLANNED`` when the run is unknown."""
    return statuses.get(run_id, RunStatus.PLANNED)


def _is_executable_run(status: RunStatus) -> bool:
    """True when a run is in a state the execute gate would let run now."""
    return status in (RunStatus.PLANNED, RunStatus.REVERTED, RunStatus.SNAPSHOTTED)


def next_executable(steps: list[StepDef], statuses: dict[str, RunStatus]) -> int | None:
    """The first step that may execute now, or None when the task is done/stuck.

    A step is executable when its run is in a pre-execution state
    (``PLANNED``/``REVERTED``/``SNAPSHOTTED``) AND every earlier step's run has
    already executed (``EXECUTED``/``VERIFIED``). Steps after that point are
    never "next" — the task is strictly forward-ordered. Pure.
    """
    for step in ordered(steps):
        status = _status_of(statuses, step.run_id)
        if _is_executable_run(status):
            return step.step_no
        if status not in (RunStatus.EXECUTED, RunStatus.VERIFIED):
            # A step whose run failed (or was never generated) blocks the task:
            # nothing later may run until the operator fixes it.
            return None
    return None


def task_revert_plan(
    steps: list[StepDef],
    statuses: dict[str, RunStatus],
    in_flight: set[str] | None = None,
) -> list[tuple[str, str]]:
    """The ordered revert targets for a WHOLE-TASK revert (pure).

    Iterate the steps in REVERSE order; per step emit its own run when live.
    Live means ``EXECUTED``/``VERIFIED``/``FAILED`` — a ``PLANNED``/``REVERTED``
    run has nothing to undo and is skipped. There is no separate apply target:
    a reviewed Run writes into its own run's manifest (and only the suggestion
    statuses), so reverting that one run undoes it along with everything else
    the run wrote.

    ``in_flight`` is the set of run ids with an operation in progress (the UI's
    op claim). Starting a task revert while any step run is in flight would race
    that pass' snapshots, so it raises :class:`StepOrderError`.
    """
    in_flight = in_flight or set()
    plan: list[tuple[str, str]] = []
    for step in reversed(ordered(steps)):
        step_status = _status_of(statuses, step.run_id)
        if step.run_id in in_flight:
            raise StepOrderError(f"step {step.step_no} run {step.run_id!r} is in flight; wait for it to finish")
        if step_status in _LIVE_STATUSES:
            plan.append(("step", step.run_id))
    return plan


def derive_task_status(
    steps: list[StepDef],
    statuses: dict[str, RunStatus],
) -> str:
    """Derive the task-level status from its steps' run statuses (pure).

    ``planned`` — no step run has executed. ``running`` — at least one executed
    and at least one still pending. ``failed`` — a step run failed. ``reverted``
    — every step run that ever executed has been reverted. ``partially_reverted``
    — some executed steps reverted while others are still live.
    """
    step_statuses = [_status_of(statuses, step.run_id) for step in ordered(steps)]
    if not step_statuses:
        return TASK_PLANNED
    if any(status == RunStatus.FAILED for status in step_statuses):
        return TASK_FAILED
    executed = [status for status in step_statuses if status in (RunStatus.EXECUTED, RunStatus.VERIFIED)]
    reverted = [status for status in step_statuses if status == RunStatus.REVERTED]
    if not executed and not reverted:
        return TASK_PLANNED
    if executed and not reverted:
        return TASK_RUNNING
    if reverted and not executed:
        return TASK_REVERTED
    return TASK_PARTIALLY_REVERTED


def build_task_view(
    task: TaskDef,
    statuses: dict[str, RunStatus],
) -> TaskView:
    """Assemble a :class:`TaskView` from a task and the live run statuses (pure)."""
    steps = [
        StepView(
            step_no=step.step_no,
            title=step.title,
            description=step.description,
            run_id=step.run_id,
            status=statuses.get(step.run_id),
        )
        for step in ordered(task.steps)
    ]
    return TaskView(
        task_id=task.task_id,
        name=task.name,
        created_at=task.created_at,
        steps=steps,
        status=derive_task_status(task.steps, statuses),
        next_step=next_executable(task.steps, statuses),
        revert_targets=task_revert_plan(task.steps, statuses),
    )


# Re-exported for readers of this module (the gate vocabulary is here, not in the
# manifest module).
TaskStatus = Literal["planned", "running", "partially_reverted", "reverted", "failed"]

__all__ = [
    "StepDef",
    "StepOrderError",
    "StepView",
    "TASK_FAILED",
    "TASK_PARTIALLY_REVERTED",
    "TASK_PLANNED",
    "TASK_REVERTED",
    "TASK_RUNNING",
    "TaskDef",
    "TaskStatus",
    "TaskView",
    "build_task_view",
    "derive_task_status",
    "next_executable",
    "ordered",
    "task_revert_plan",
]
