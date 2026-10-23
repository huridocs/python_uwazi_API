"""Live E2E: multistep task creation through the service layer (no UI).

Each step is one real run, generated into a deterministic planning script and
scanned by the real automatic pass. The task layer is the production
``TaskUseCase`` wired to the same ``revert_run`` the web driver uses.
"""

from __future__ import annotations

import pytest

from uwazi_admin_agent.domain.manifest import RunStatus
from uwazi_admin_agent.domain.task import TASK_PLANNED, TASK_RUNNING
from uwazi_admin_agent.tests.e2e_support import (
    DEFAULT_PROPERTY,
    UWAZI_URL,
    AdminE2EWorkspace,
    update_script,
)
from uwazi_api.testing.e2e_guard import require_local_e2e_target

pytestmark = pytest.mark.e2e


@pytest.fixture(autouse=True)
def _live_guard() -> None:
    require_local_e2e_target(UWAZI_URL)


def test_single_step_task_persists_one_planned_run(workspace: AdminE2EWorkspace) -> None:
    """A one-step task stores its definition and scans exactly one row."""
    ws = workspace
    shared_id = ws.entity_ids[0]
    run_id = ws.next_run_id("single")
    script = update_script(ws.template_name, [shared_id], ws.languages.default, ws.value("single"))
    task_id = ws.next_task_id("single")

    task = ws.create_task(
        task_id,
        "single-step task",
        [(run_id, "update the default row", "deterministic test prompt")],
        {run_id: script},
    )

    assert task.task_id == task_id
    view = ws.task_view(task_id)
    assert view.status == TASK_PLANNED
    assert len(view.steps) == 1
    assert view.steps[0].run_id == run_id
    assert view.steps[0].status == RunStatus.PLANNED
    assert view.next_step == 1
    assert view.revert_targets == []

    rows = ws.suggestions(run_id)
    assert [(row.shared_id, row.language, row.property_name) for row in rows] == [
        (shared_id, ws.languages.default, DEFAULT_PROPERTY)
    ]
    assert rows[0].proposed_value == ws.value("single")
    assert ws.status(run_id) == RunStatus.PLANNED


def test_two_step_task_advances_in_forward_order(workspace: AdminE2EWorkspace) -> None:
    """After step 1 executes, only step 2 is next; after step 2, nothing is next."""
    ws = workspace
    first_id, second_id = ws.entity_ids[:2]
    first_run = ws.next_run_id("first")
    second_run = ws.next_run_id("second")
    task_id = ws.next_task_id("different-entities")

    task = ws.create_task(
        task_id,
        "two entities, two steps",
        [
            (first_run, "update the first entity", "first deterministic step"),
            (second_run, "update the second entity", "second deterministic step"),
        ],
        {
            first_run: update_script(ws.template_name, [first_id], ws.languages.default, ws.value("first")),
            second_run: update_script(ws.template_name, [second_id], ws.languages.default, ws.value("second")),
        },
    )
    assert task.task_id == task_id
    view = ws.task_view(task_id)
    assert view.status == TASK_PLANNED
    assert view.next_step == 1
    assert [step.status for step in view.steps] == [RunStatus.PLANNED, RunStatus.PLANNED]

    ws.apply(first_run)
    view = ws.task_view(task_id)
    assert view.status == TASK_RUNNING
    assert [step.status for step in view.steps] == [RunStatus.EXECUTED, RunStatus.PLANNED]
    assert view.next_step == 2
    assert view.revert_targets == [("step", first_run)]
    assert ws.text(first_id, ws.languages.default) == ws.value("first")
    assert ws.text(second_id, ws.languages.default) == "original-1"

    ws.apply(second_run)
    view = ws.task_view(task_id)
    assert view.status == TASK_RUNNING
    assert [step.status for step in view.steps] == [RunStatus.EXECUTED, RunStatus.EXECUTED]
    assert view.next_step is None
    assert view.revert_targets == [("step", second_run), ("step", first_run)]
    assert ws.text(second_id, ws.languages.default) == ws.value("second")


def test_two_step_task_can_target_the_same_entity(workspace: AdminE2EWorkspace) -> None:
    """A later step plans over the state the task starts from and overwrites it."""
    ws = workspace
    shared_id = ws.entity_ids[0]
    first_run = ws.next_run_id("first")
    second_run = ws.next_run_id("second")
    task_id = ws.next_task_id("same-entity")

    ws.create_task(
        task_id,
        "one entity, two steps",
        [
            (first_run, "write the first value", "first deterministic step"),
            (second_run, "write the second value", "second deterministic step"),
        ],
        {
            first_run: update_script(ws.template_name, [shared_id], ws.languages.default, ws.value("one")),
            second_run: update_script(ws.template_name, [shared_id], ws.languages.default, ws.value("two")),
        },
    )

    ws.apply(first_run)
    assert ws.text(shared_id, ws.languages.default) == ws.value("one")

    ws.apply(second_run)
    assert ws.text(shared_id, ws.languages.default) == ws.value("two")

    view = ws.task_view(task_id)
    assert view.status == TASK_RUNNING
    assert [step.status for step in view.steps] == [RunStatus.EXECUTED, RunStatus.EXECUTED]
    assert len(view.revert_targets) == 2


def test_add_existing_run_appends_the_next_step(workspace: AdminE2EWorkspace) -> None:
    """An already-created run can be linked as the next task step."""
    ws = workspace
    shared_id = ws.entity_ids[0]
    existing_run = ws.new_run(
        update_script(ws.template_name, [shared_id], ws.languages.default, ws.value("existing")),
        prompt="an existing deterministic run",
    )
    own_run = ws.next_run_id("own")
    task_id = ws.next_task_id("add-existing")

    ws.create_task(
        task_id,
        "task with an existing run appended",
        [(own_run, "the task's own step", "deterministic task step")],
        {own_run: update_script(ws.template_name, [shared_id], ws.languages.default, ws.value("own"))},
    )

    updated = ws.add_existing_run(task_id, existing_run, "the pre-existing run", "appended by the operator")
    assert [step.run_id for step in updated.steps] == [own_run, existing_run]

    view = ws.task_view(task_id)
    assert len(view.steps) == 2
    assert [step.status for step in view.steps] == [RunStatus.PLANNED, RunStatus.PLANNED]
    assert view.next_step == 1
