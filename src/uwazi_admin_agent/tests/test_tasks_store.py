"""Isolated unit tests for the YAML task store (per AGENTS.md).

Real filesystem under pytest's ``tmp_path`` (local, offline, deterministic): real
``YamlTaskStore``, real ``TaskDef``/``StepDef`` models, plain assertions. No mocks.
"""

from datetime import datetime, timezone

import pytest

from uwazi_admin_agent.adapters.tasks_store import YamlTaskStore, new_task
from uwazi_admin_agent.domain.task import StepDef, TaskDef

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _steps(*run_ids: str) -> list[StepDef]:
    return [
        StepDef(step_no=index + 1, title=f"Step {index + 1}", description=f"does {run_id}", run_id=run_id)
        for index, run_id in enumerate(run_ids)
    ]


@pytest.fixture()
def store(tmp_path) -> YamlTaskStore:
    return YamlTaskStore(tmp_path / "tasks")


def test_save_then_load_round_trips(store: YamlTaskStore) -> None:
    task = TaskDef(task_id="t1", name="Merge then enrich", steps=_steps("r1", "r2"), created_at=NOW)
    store.save_task(task)
    loaded = store.load_task("t1")
    assert loaded == task
    assert [s.run_id for s in loaded.steps] == ["r1", "r2"]
    assert loaded.steps[1].description == "does r2"


def test_load_missing_raises(store: YamlTaskStore) -> None:
    with pytest.raises(FileNotFoundError):
        store.load_task("nope")


def test_save_overwrites(store: YamlTaskStore) -> None:
    store.save_task(TaskDef(task_id="t1", name="A", steps=_steps("r1"), created_at=NOW))
    store.save_task(TaskDef(task_id="t1", name="B", steps=_steps("r1", "r2"), created_at=NOW))
    loaded = store.load_task("t1")
    assert loaded.name == "B"
    assert len(loaded.steps) == 2


def test_list_tasks_returns_every_saved_task(store: YamlTaskStore) -> None:
    store.save_task(TaskDef(task_id="t1", name="One", steps=_steps("r1"), created_at=NOW))
    store.save_task(TaskDef(task_id="t2", name="Two", steps=_steps("r2"), created_at=NOW))
    assert {t.task_id for t in store.list_tasks()} == {"t1", "t2"}


def test_list_tasks_on_missing_root_is_empty(tmp_path) -> None:
    assert YamlTaskStore(tmp_path / "never-created").list_tasks() == []


def test_list_tasks_skips_an_unreadable_file(store: YamlTaskStore, tmp_path) -> None:
    store.save_task(TaskDef(task_id="t1", name="One", steps=_steps("r1"), created_at=NOW))
    (tmp_path / "tasks" / "broken.yaml").write_text("not: [a: valid: task", encoding="utf-8")
    tasks = store.list_tasks()
    assert [t.task_id for t in tasks] == ["t1"]


def test_add_step_appends_and_persists(store: YamlTaskStore) -> None:
    store.save_task(TaskDef(task_id="t1", name="One", steps=_steps("r1"), created_at=NOW))
    updated = store.add_step("t1", StepDef(step_no=2, title="Step 2", description="", run_id="r2"))
    assert [s.run_id for s in updated.steps] == ["r1", "r2"]
    assert [s.run_id for s in store.load_task("t1").steps] == ["r1", "r2"]


def test_delete_task_removes_the_file(store: YamlTaskStore) -> None:
    store.save_task(TaskDef(task_id="t1", name="One", steps=_steps("r1"), created_at=NOW))
    store.delete_task("t1")
    with pytest.raises(FileNotFoundError):
        store.load_task("t1")
    store.delete_task("t1")  # idempotent


def test_path_traversal_is_rejected(store: YamlTaskStore) -> None:
    with pytest.raises(ValueError):
        store.save_task(TaskDef(task_id="..", name="x", steps=[], created_at=NOW))


def test_a_slash_in_the_id_stays_inside_the_root(store: YamlTaskStore, tmp_path) -> None:
    """A task id with a separator must be sanitized, not escape the store root."""
    task = TaskDef(task_id="a/b", name="x", steps=_steps("r1"), created_at=NOW)
    store.save_task(task)
    store.save_task(task.model_copy(update={"task_id": "a/b"}))
    # Written under the sanitized name inside the root, and readable back.
    assert (tmp_path / "tasks" / "a_b.yaml").is_file()
    assert store.load_task("a/b").name == "x"


def test_new_task_stamps_creation_time(store: YamlTaskStore) -> None:
    task = new_task("t9", "Nine", _steps("r1"))
    assert task.task_id == "t9"
    assert task.created_at.tzinfo is not None


def test_step_numbers_and_descriptions_survive_yaml(store: YamlTaskStore) -> None:
    task = TaskDef(
        task_id="t1",
        name="Multi",
        steps=[
            StepDef(step_no=1, title="First", description="multi\nline\ndescription", run_id="r1"),
            StepDef(step_no=2, title="Second", description="", run_id="r2"),
        ],
        created_at=NOW,
    )
    store.save_task(task)
    loaded = store.load_task("t1")
    assert loaded.steps[0].description == "multi\nline\ndescription"
    assert [s.step_no for s in loaded.steps] == [1, 2]
