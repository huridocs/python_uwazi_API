"""The on-disk layout of ONE Uwazi instance.

Pure path math — no I/O, no filesystem access. Every path the admin agent
writes for an instance lives under ``data/instances/<slug>/``, so two
instances never share a run, a task, a prompt or a suggestions database.

An EMPTY URL is refused: it used to produce a shared ``data/instances/_<hash>``
bucket (and, in the builders that fall back to the first configured instance,
another instance's tree). A caller with no session has no instance to serve,
so it gets a clear error instead of someone else's data.
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


def require_instance_url(url: str | None) -> str:
    """Return ``url`` stripped, or raise when there is no instance to target.

    The web layer must never fall back to "the first configured instance": a
    logged-out or mid-switch session would otherwise read another instance's
    runs, tasks and executions.
    """
    value = (url or "").strip()
    if not value:
        raise ValueError("No Uwazi instance is bound to this session")
    return value


def instance_paths(url: str, instances_root: Path) -> InstancePaths:
    """The paths of one instance, derived from its URL and the instances root.

    Pure: builds the paths, creates nothing. Raises :class:`ValueError` for an
    empty URL, so an unbound caller cannot silently share another instance's
    tree.
    """
    url = require_instance_url(url)
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
