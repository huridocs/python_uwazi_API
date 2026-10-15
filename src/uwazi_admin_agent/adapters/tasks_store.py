"""YAML persistence for multistep task definitions (one file per task).

Mirrors the repo's existing YAML conventions (``data/prompts/<name>.yaml``,
``data/active_run.yaml``): a task is one small YAML file under ``TASKS_PATH``,
human-readable and diffable. Nothing about a task's runtime STATE lives here —
step statuses come from the runs' own manifests, so this store only defines
what the task IS (its ordered steps).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import override

import yaml
from loguru import logger

from uwazi_admin_agent.domain.task import StepDef, TaskDef
from uwazi_admin_agent.ports.task_store_port import TaskStorePort


class YamlTaskStore(TaskStorePort):
    """Filesystem task store: ``<root>/<task_id>.yaml`` per task."""

    def __init__(self, root: Path) -> None:
        self._root: Path = Path(root)

    # --- helpers -------------------------------------------------------------

    def _path(self, task_id: str) -> Path:
        safe = task_id.strip().replace("/", "_").replace("\\", "_")
        if not safe or safe in (".", ".."):
            raise ValueError(f"Invalid task id: {task_id!r}")
        return self._root / f"{safe}.yaml"

    @override
    def save_task(self, task: TaskDef) -> None:
        path = self._path(task.task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = task.model_dump(mode="json")
        path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
        logger.debug("task saved id={} steps={}", task.task_id, len(task.steps))

    @override
    def load_task(self, task_id: str) -> TaskDef:
        path = self._path(task_id)
        if not path.is_file():
            raise FileNotFoundError(f"No task file for {task_id!r}")
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return TaskDef.model_validate(data)

    @override
    def list_tasks(self) -> list[TaskDef]:
        if not self._root.is_dir():
            return []
        tasks: list[TaskDef] = []
        for path in sorted(self._root.glob("*.yaml")):
            try:
                tasks.append(TaskDef.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {}))
            except Exception as exc:  # noqa: BLE001 — one broken file must not hide the rest
                logger.warning("skipping unreadable task file {}: {}", path, exc)
        return tasks

    @override
    def add_step(self, task_id: str, step: StepDef) -> TaskDef:
        task = self.load_task(task_id)
        updated = task.model_copy(update={"steps": [*task.steps, step]})
        self.save_task(updated)
        return updated

    @override
    def delete_task(self, task_id: str) -> None:
        path = self._path(task_id)
        if path.is_file():
            path.unlink()
        logger.debug("task deleted id={}", task_id)


def new_task(task_id: str, name: str, steps: list[StepDef]) -> TaskDef:
    """Build a task definition with the current timestamp (pure)."""
    return TaskDef(task_id=task_id, name=name, steps=steps, created_at=datetime.now(timezone.utc))


__all__ = ["YamlTaskStore", "new_task"]
