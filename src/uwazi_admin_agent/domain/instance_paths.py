"""The on-disk layout of ONE Uwazi instance.

Pure path math — no I/O, no filesystem access. Every path the admin agent
writes for an instance lives under ``data/instances/<slug>/``, so two
instances never share a run, a task, a prompt or a suggestions database.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from uwazi_admin_agent.domain.instance import instance_slug


@dataclass(frozen=True)
class InstancePaths:
    """Every on-disk path that belongs to ONE Uwazi instance."""

    instance_id: str
    url: str
    root: Path
    runs_file: Path
    prompts_path: Path
    runs_path: Path
    tasks_path: Path
    suggestions_db: Path


def instance_paths(url: str, instances_root: Path) -> InstancePaths:
    """The paths of one instance, derived from its URL and the instances root.

    Pure: builds the paths, creates nothing.
    """
    slug = instance_slug(url)
    root = Path(instances_root) / slug
    return InstancePaths(
        instance_id=slug,
        url=url,
        root=root,
        runs_file=root / "active_run.yaml",
        prompts_path=root / "prompts",
        runs_path=root / "runs",
        tasks_path=root / "tasks",
        suggestions_db=root / "suggestions.db",
    )
