"""Isolated tests for the multistep task use case (per AGENTS.md).

Real objects: a real ``YamlTaskStore`` on ``tmp_path``, a real
``FilesystemBackupStore`` for manifests, and recording async callables standing
in for the run operations (they are the injected seam by design — the use case is
pure orchestration, so the callables ARE its interface and a real in-memory
implementation is the correct test double, not a mock).
"""

from datetime import datetime, timezone
from typing import Any

import pytest

from uwazi_admin_agent.adapters.backup_store_adapter import FilesystemBackupStore
from uwazi_admin_agent.adapters.tasks_store import YamlTaskStore
from uwazi_admin_agent.domain.manifest import MigrationManifest, RunStatus
from uwazi_admin_agent.domain.snapshot import EntityIdentity
from uwazi_admin_agent.domain.task import StepDef, StepOrderError, TaskDef
from uwazi_admin_agent.use_cases.task_use_case import TaskUseCase

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class RecordingOps:
    """Real, minimal implementations of the injected run operations."""

    def __init__(self, backup: FilesystemBackupStore) -> None:
        self.backup = backup
        self.reverted: list[str] = []
        self.created: list[str] = []
        self.prompts: dict[str, str] = {}
        self.fail_revert_on: str | None = None

    async def revert_run(self, run_id: str, user: str, password: str) -> None:
        if self.fail_revert_on == run_id:
            raise RuntimeError("revert failed")
        self.reverted.append(run_id)
        self._mark(run_id, RunStatus.REVERTED)

    async def create_run(self, run_id: str, prompt: str, user: str, password: str) -> None:
        """The real create signature: ``(run_id, prompt, user, password)``."""
        self.created.append(run_id)
        self.prompts[run_id] = prompt
        self._mark(run_id, RunStatus.PLANNED)

    def _mark(self, run_id: str, status: RunStatus) -> None:
        manifest = MigrationManifest(
            run_id=run_id,
            created_at=NOW,
            prompt="p",
            script="s",
            status=status,
            modified=[EntityIdentity(shared_id="x", title="X", template_name="T")] if status == RunStatus.EXECUTED else [],
        )
        self.backup.save_manifest(run_id, manifest)


def _use_case(tmp_path) -> tuple[TaskUseCase, RecordingOps, YamlTaskStore]:
    tasks = YamlTaskStore(tmp_path / "tasks")
    backup = FilesystemBackupStore(tmp_path / "runs")
    ops = RecordingOps(backup)
    use_case = TaskUseCase(
        task_store=tasks,
        backup_store=backup,
        revert_run=ops.revert_run,
        create_run=ops.create_run,
    )
    return use_case, ops, tasks


def _steps(*run_ids: str) -> list[StepDef]:
    return [
        StepDef(step_no=index + 1, title=f"Step {index + 1}", description="", run_id=run_id)
        for index, run_id in enumerate(run_ids)
    ]


# --- creation ----------------------------------------------------------------


@pytest.mark.anyio
async def test_create_task_creates_one_run_per_step(tmp_path) -> None:
    use_case, ops, tasks = _use_case(tmp_path)
    task = await use_case.create_task(
        "t1",
        "Two steps",
        [("r1", "First", "do one"), ("r2", "Second", "do two")],
        user="u",
        password="p",
    )
    assert ops.created == ["r1", "r2"]
    assert ops.prompts == {"r1": "do one", "r2": "do two"}
    assert [s.run_id for s in task.steps] == ["r1", "r2"]
    assert [s.step_no for s in task.steps] == [1, 2]
    assert [s.description for s in task.steps] == ["do one", "do two"]
    assert tasks.load_task("t1").name == "Two steps"


@pytest.mark.anyio
async def test_add_existing_run_appends_a_step(tmp_path) -> None:
    use_case, _ops, tasks = _use_case(tmp_path)
    tasks.save_task(TaskDef(task_id="t1", name="T", steps=_steps("r1"), created_at=NOW))
    updated = use_case.add_existing_run("t1", "r2", "Second")
    assert [s.step_no for s in updated.steps] == [1, 2]
    assert updated.steps[1].run_id == "r2"


# --- views -------------------------------------------------------------------


@pytest.mark.anyio
async def test_view_reports_statuses_next_step_and_plan(tmp_path) -> None:
    use_case, ops, _tasks = _use_case(tmp_path)
    await use_case.create_task("t1", "T", [("r1", "A", ""), ("r2", "B", "")], user="u", password="p")
    ops._mark("r1", RunStatus.EXECUTED)  # step 1 executed
    view = use_case.get_task("t1")
    assert view.status == "running"
    assert view.next_step == 2
    assert view.steps[0].status == RunStatus.EXECUTED
    assert view.revert_targets == [("step", "r1")]


@pytest.mark.anyio
async def test_view_has_no_apply_run(tmp_path) -> None:
    """A reviewed Run writes into the step's own run: no second run, no extra row."""
    use_case, ops, _tasks = _use_case(tmp_path)
    await use_case.create_task("t1", "T", [("r1", "A", "")], user="u", password="p")
    ops._mark("r1", RunStatus.EXECUTED)
    view = use_case.get_task("t1")
    assert not hasattr(view.steps[0], "apply_run_id")
    assert not hasattr(view.steps[0], "apply_status")
    assert view.revert_targets == [("step", "r1")]


# --- whole-task revert -------------------------------------------------------


@pytest.mark.anyio
async def test_revert_task_reverts_in_reverse_order(tmp_path) -> None:
    use_case, ops, _tasks = _use_case(tmp_path)
    await use_case.create_task("t1", "T", [("r1", "A", ""), ("r2", "B", "")], user="u", password="p")
    ops._mark("r1", RunStatus.EXECUTED)
    ops._mark("r2", RunStatus.EXECUTED)
    reverted = await use_case.revert_task("t1", "u", "p")
    assert reverted == ["r2", "r1"]
    assert ops.reverted == ["r2", "r1"]


@pytest.mark.anyio
async def test_revert_task_reverts_a_reviewed_step_run_once(tmp_path) -> None:
    """The step run carries the reviewed writes, so one target undoes them all."""
    use_case, ops, _tasks = _use_case(tmp_path)
    await use_case.create_task("t1", "T", [("r1", "A", "")], user="u", password="p")
    ops._mark("r1", RunStatus.EXECUTED)
    reverted = await use_case.revert_task("t1", "u", "p")
    assert reverted == ["r1"]


@pytest.mark.anyio
async def test_revert_task_stops_at_a_failure(tmp_path) -> None:
    use_case, ops, _tasks = _use_case(tmp_path)
    await use_case.create_task("t1", "T", [("r1", "A", ""), ("r2", "B", "")], user="u", password="p")
    ops._mark("r1", RunStatus.EXECUTED)
    ops._mark("r2", RunStatus.EXECUTED)
    ops.fail_revert_on = "r1"
    with pytest.raises(RuntimeError, match="revert failed"):
        await use_case.revert_task("t1", "u", "p")
    # r2 reverted, r1 attempted and failed: the remainder is visible for retry.
    assert ops.reverted == ["r2"]
    view = use_case.get_task("t1")
    assert view.revert_targets == [("step", "r1")]


@pytest.mark.anyio
async def test_revert_task_with_nothing_executed_is_refused(tmp_path) -> None:
    use_case, _ops, _tasks = _use_case(tmp_path)
    await use_case.create_task("t1", "T", [("r1", "A", "")], user="u", password="p")
    with pytest.raises(StepOrderError, match="nothing to revert"):
        await use_case.revert_task("t1", "u", "p")


@pytest.mark.anyio
async def test_revert_task_refuses_while_a_step_is_in_flight(tmp_path) -> None:
    use_case, _ops, _tasks = _use_case(tmp_path)
    await use_case.create_task("t1", "T", [("r1", "A", "")], user="u", password="p")
    with pytest.raises(StepOrderError, match="in flight"):
        await use_case.revert_task("t1", "u", "p", in_flight={"r1"})


@pytest.mark.anyio
async def test_reverted_steps_drop_out_of_the_plan(tmp_path) -> None:
    use_case, ops, _tasks = _use_case(tmp_path)
    await use_case.create_task("t1", "T", [("r1", "A", "")], user="u", password="p")
    ops._mark("r1", RunStatus.EXECUTED)
    await use_case.revert_task("t1", "u", "p")
    assert use_case.get_task("t1").revert_targets == []
    assert ops.reverted == ["r1"]


# --- listing / deletion ------------------------------------------------------


@pytest.mark.anyio
async def test_list_tasks_is_newest_first(tmp_path) -> None:
    use_case, _ops, tasks = _use_case(tmp_path)
    tasks.save_task(TaskDef(task_id="old", name="Old", steps=[], created_at=datetime(2025, 1, 1, tzinfo=timezone.utc)))
    tasks.save_task(TaskDef(task_id="new", name="New", steps=[], created_at=datetime(2026, 1, 1, tzinfo=timezone.utc)))
    assert [t.task_id for t in use_case.list_tasks()] == ["new", "old"]


@pytest.mark.anyio
async def test_delete_task_removes_it(tmp_path) -> None:
    use_case, _ops, tasks = _use_case(tmp_path)
    tasks.save_task(TaskDef(task_id="t1", name="T", steps=[], created_at=NOW))
    use_case.delete_task("t1")
    assert use_case.list_tasks() == []


def test_task_view_on_an_unknown_task_raises(tmp_path) -> None:
    use_case, _ops, _tasks = _use_case(tmp_path)
    with pytest.raises(FileNotFoundError):
        use_case.get_task("missing")


@pytest.fixture()
def anyio_backend() -> Any:
    return "asyncio"
