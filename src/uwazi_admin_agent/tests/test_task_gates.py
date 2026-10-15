"""Isolated unit tests for the multistep-task gates (per AGENTS.md).

Pure: literal ``StepDef`` lists + status dicts, real :class:`RunStatus` values.
No mocks, no I/O, no instance.
"""

from datetime import datetime, timezone

import pytest

from uwazi_admin_agent.domain.manifest import RunStatus
from uwazi_admin_agent.domain.task import (
    TASK_FAILED,
    TASK_PARTIALLY_REVERTED,
    TASK_PLANNED,
    TASK_REVERTED,
    TASK_RUNNING,
    StepDef,
    StepOrderError,
    TaskDef,
    build_task_view,
    derive_task_status,
    next_executable,
    ordered,
    task_revert_plan,
)


def _steps(*run_ids: str) -> list[StepDef]:
    return [
        StepDef(step_no=index + 1, title=f"Step {index + 1}", description="", run_id=run_id)
        for index, run_id in enumerate(run_ids)
    ]


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


# --- ordered -----------------------------------------------------------------


def test_ordered_sorts_by_step_no_without_mutating_input() -> None:
    steps = [
        StepDef(step_no=3, title="c", description="", run_id="r3"),
        StepDef(step_no=1, title="a", description="", run_id="r1"),
        StepDef(step_no=2, title="b", description="", run_id="r2"),
    ]
    assert [s.step_no for s in ordered(steps)] == [1, 2, 3]
    assert [s.step_no for s in steps] == [3, 1, 2]


# --- next_executable ---------------------------------------------------------


def test_next_executable_is_the_first_planned_step() -> None:
    steps = _steps("r1", "r2", "r3")
    assert next_executable(steps, {}) == 1
    assert next_executable(steps, {"r1": RunStatus.EXECUTED}) == 2
    assert next_executable(steps, {"r1": RunStatus.EXECUTED, "r2": RunStatus.EXECUTED}) == 3


def test_next_executable_is_none_when_all_steps_done() -> None:
    steps = _steps("r1", "r2")
    assert next_executable(steps, {"r1": RunStatus.EXECUTED, "r2": RunStatus.VERIFIED}) is None


def test_next_executable_allows_reexecution_after_revert() -> None:
    steps = _steps("r1", "r2")
    # r1 reverted, r2 still planned: r1 is the working cycle's next target again.
    assert next_executable(steps, {"r1": RunStatus.REVERTED}) == 1


def test_next_executable_stops_at_a_failed_step() -> None:
    steps = _steps("r1", "r2", "r3")
    # r1 failed: neither r1 (not executable) nor r2/r3 (order blocked) is next.
    assert next_executable(steps, {"r1": RunStatus.FAILED}) is None


def test_next_executable_stops_at_a_generation_failed_step() -> None:
    steps = _steps("r1", "r2")
    assert next_executable(steps, {"r1": RunStatus.GENERATION_FAILED}) is None


def test_next_executable_on_empty_task() -> None:
    assert next_executable([], {}) is None


# --- task_revert_plan --------------------------------------------------------


def test_revert_plan_is_reverse_order_step_run_per_step() -> None:
    steps = _steps("r1", "r2")
    statuses = {"r1": RunStatus.EXECUTED, "r2": RunStatus.EXECUTED}
    assert task_revert_plan(steps, statuses) == [("step", "r2"), ("step", "r1")]


def test_revert_plan_skips_planned_and_reverted_runs() -> None:
    steps = _steps("r1", "r2", "r3")
    statuses = {"r1": RunStatus.REVERTED, "r2": RunStatus.EXECUTED}
    assert task_revert_plan(steps, statuses) == [("step", "r2")]


def test_revert_plan_includes_failed_runs() -> None:
    steps = _steps("r1")
    assert task_revert_plan(steps, {"r1": RunStatus.FAILED}) == [("step", "r1")]


def test_revert_plan_refuses_while_a_step_run_is_in_flight() -> None:
    steps = _steps("r1", "r2")
    with pytest.raises(StepOrderError) as excinfo:
        task_revert_plan(steps, {"r2": RunStatus.EXECUTED}, in_flight={"r1"})
    assert "in flight" in str(excinfo.value)


def test_revert_plan_has_no_apply_run_target() -> None:
    """A reviewed Run writes into its step's run: the plan never names another run."""
    steps = _steps("r1")
    plan = task_revert_plan(steps, {"r1": RunStatus.EXECUTED})
    assert plan == [("step", "r1")]
    assert all(kind == "step" for kind, _run_id in plan)


def test_revert_plan_empty_when_nothing_executed() -> None:
    assert task_revert_plan(_steps("r1", "r2"), {}) == []


# --- derive_task_status ------------------------------------------------------


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ({}, TASK_PLANNED),
        ({"r1": RunStatus.EXECUTED}, TASK_RUNNING),
        ({"r1": RunStatus.EXECUTED, "r2": RunStatus.EXECUTED}, TASK_RUNNING),
        ({"r1": RunStatus.REVERTED}, TASK_REVERTED),
        ({"r1": RunStatus.REVERTED, "r2": RunStatus.REVERTED}, TASK_REVERTED),
        ({"r1": RunStatus.REVERTED, "r2": RunStatus.EXECUTED}, TASK_PARTIALLY_REVERTED),
        ({"r1": RunStatus.FAILED}, TASK_FAILED),
        ({"r1": RunStatus.EXECUTED, "r2": RunStatus.FAILED}, TASK_FAILED),
    ],
)
def test_derive_task_status_matrix(statuses: dict[str, RunStatus], expected: str) -> None:
    assert derive_task_status(_steps("r1", "r2"), statuses) == expected


def test_derive_task_status_on_empty_task() -> None:
    assert derive_task_status([], {}) == TASK_PLANNED


# --- build_task_view ---------------------------------------------------------


def test_build_task_view_assembles_steps_status_and_plan() -> None:
    task = TaskDef(task_id="t1", name="Merge then enrich", steps=_steps("r1", "r2"), created_at=NOW)
    statuses = {"r1": RunStatus.EXECUTED}
    view = build_task_view(task, statuses)
    assert (view.task_id, view.name) == ("t1", "Merge then enrich")
    assert [s.run_id for s in view.steps] == ["r1", "r2"]
    assert view.steps[0].status == RunStatus.EXECUTED
    assert view.steps[1].status is None  # run not present in the status map
    assert view.next_step == 2
    assert view.status == TASK_RUNNING
    assert view.revert_targets == [("step", "r1")]
