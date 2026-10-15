"""Persistence seam for multistep task definitions.

A task's *definition* (its ordered steps) is persisted; its *state* is not —
step statuses are read from the runs' own manifests, so there is exactly one
source of truth for "what has executed" and no state to reconcile.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from uwazi_admin_agent.domain.task import StepDef, TaskDef


class TaskStorePort(ABC):
    """Storage API for task definitions."""

    @abstractmethod
    def save_task(self, task: TaskDef) -> None:
        """Create or overwrite a task's definition file."""
        ...

    @abstractmethod
    def load_task(self, task_id: str) -> TaskDef:
        """Load one task definition (raises ``FileNotFoundError`` when absent)."""
        ...

    @abstractmethod
    def list_tasks(self) -> list[TaskDef]:
        """Every task definition (unreadable files are skipped with a warning)."""
        ...

    @abstractmethod
    def add_step(self, task_id: str, step: StepDef) -> TaskDef:
        """Append a step to a task and return the updated definition."""
        ...

    @abstractmethod
    def delete_task(self, task_id: str) -> None:
        """Remove a task's definition file (its runs are deleted separately)."""
        ...


__all__ = ["TaskStorePort"]
