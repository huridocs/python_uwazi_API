"""Web driver module split out of the original ``run_service`` (behavior preserved)."""

from __future__ import annotations

from typing import Any

from uwazi_admin_agent.adapters.tasks_store import YamlTaskStore
from uwazi_admin_agent.domain.task import TaskView
from uwazi_admin_agent.drivers.runtime import build_backup_store
from uwazi_admin_agent.ports.task_store_port import TaskStorePort

from .generation import create_and_generate
from .revert import revert_run
from .wiring import _paths


def build_task_store(url: str) -> TaskStorePort:
    """Return the YAML task store under the instance's ``tasks/`` directory."""
    return YamlTaskStore(_paths(url).tasks_path)


def _task_use_case(url: str) -> Any:
    """Wire the task use case over the existing single-run functions.

    The task layer is pure orchestration, so it is handed the very same
    revert/create functions the single-run flow uses — one code
    path for the actual work, no second implementation to keep in sync. The
    instance URL is bound here, so the use case keeps the single-run
    signatures it already knows.
    """
    from uwazi_admin_agent.use_cases.task_use_case import TaskUseCase

    return TaskUseCase(
        task_store=build_task_store(url),
        backup_store=build_backup_store(url),
        revert_run=lambda run_id, user, password: revert_run(url, run_id, user, password),
        create_run=lambda name, prompt, user, password: create_and_generate(url, name, prompt, user, password),
    )


def task_view(url: str, task_id: str) -> TaskView:
    """A task's live view: steps, statuses, next executable step, revert plan."""
    return _task_use_case(url).get_task(task_id)
