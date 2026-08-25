"""Async service layer for the admin agent web UI (Phase 5 driver).

Returns data (not prints) so NiceGUI's event loop can call mutating operations
in background tasks. Calls use cases directly — the step drivers
(``step_generate`` etc.) wrap each in ``asyncio.run()``, which is incompatible
with NiceGUI's already-running loop, so this module mirrors their wiring
without the ``asyncio.run`` wrapper.

No business logic: this is a driver that wires adapters to use cases, matching
the ``drivers/`` layer convention.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import yaml

from uwazi_admin_agent.adapters.runs_config_loader import RunsConfigLoader
from uwazi_admin_agent.adapters.script_emitter import emit_generated_script
from uwazi_admin_agent.configuration import (
    DEFAULT_ON_ERROR_POLICY,
    DUMMY_LANGUAGE,
    MAX_ENTITIES_PER_RUN,
    PROMPTS_PATH,
    RUNS_FILE,
    RUNS_PATH,
)
from uwazi_admin_agent.domain.manifest import MigrationManifest, RunStatus
from uwazi_admin_agent.domain.on_error_policy import OnErrorPolicy
from uwazi_admin_agent.drivers.runtime import build_backup_store, build_runtime
from uwazi_admin_agent.use_cases.execute_script_use_case import ExecuteScriptUseCase
from uwazi_admin_agent.use_cases.generate_script_use_case import GenerateScriptUseCase


@dataclass(frozen=True)
class RunSummary:
    """One row in the runs table — the manifest's headline fields."""

    run_id: str
    status: RunStatus
    created_at: datetime
    prompt: str
    modified: int
    deleted: int
    created: int
    rewired: int


@dataclass(frozen=True)
class RunDetail:
    """A run's manifest plus its emitted script source (for inspect views)."""

    run_id: str
    status: RunStatus
    created_at: datetime
    prompt: str
    script: str
    modified: int
    deleted: int
    created: int
    rewired: int


def list_runs() -> list[RunSummary]:
    """Return all persisted runs (skips folders whose manifest is unreadable)."""
    store = build_backup_store()
    summaries: list[RunSummary] = []
    for run_id in store.list_runs():
        try:
            manifest = store.load_manifest(run_id)
        except FileNotFoundError:
            continue
        summaries.append(
            RunSummary(
                run_id=manifest.run_id,
                status=manifest.status,
                created_at=manifest.created_at,
                prompt=manifest.prompt,
                modified=len(manifest.modified),
                deleted=len(manifest.deleted),
                created=len(manifest.created),
                rewired=len(manifest.rewired),
            )
        )
    return summaries


def get_run(run_id: str) -> RunDetail:
    """Return a run's manifest fields + the emitted script source."""
    store = build_backup_store()
    manifest = store.load_manifest(run_id)
    script_path = RUNS_PATH / run_id / "script.py"
    script = script_path.read_text(encoding="utf-8") if script_path.is_file() else manifest.script
    return RunDetail(
        run_id=manifest.run_id,
        status=manifest.status,
        created_at=manifest.created_at,
        prompt=manifest.prompt,
        script=script,
        modified=len(manifest.modified),
        deleted=len(manifest.deleted),
        created=len(manifest.created),
        rewired=len(manifest.rewired),
    )


async def create_and_generate(name: str, prompt: str) -> None:
    """Write the prompt + active-run pointer, then generate the script + manifest.

    Raises on LLM/Uwazi failure (the caller surfaces it via ``ui.notify``).
    """
    PROMPTS_PATH.mkdir(parents=True, exist_ok=True)
    (PROMPTS_PATH / f"{name}.yaml").write_text(yaml.dump({"prompt": prompt}), encoding="utf-8")
    RUNS_FILE.parent.mkdir(parents=True, exist_ok=True)
    RUNS_FILE.write_text(yaml.dump({"active_run": name}), encoding="utf-8")

    loader = RunsConfigLoader.default()
    run_path = loader.load_active_path()

    runtime = build_runtime()
    use_case = GenerateScriptUseCase(
        llm=runtime.llm,
        deps=runtime.deps,
        entity_repository=runtime.entity_repository,
    )

    script = await use_case.execute(prompt)
    emit_generated_script(script, run_path)

    manifest = MigrationManifest(
        run_id=name,
        created_at=datetime.now(timezone.utc),
        prompt=prompt,
        script=script.python_code,
        status=RunStatus.PLANNED,
        snapshot_dir=str(run_path),
    )
    runtime.backup_store.save_manifest(name, manifest)


async def execute_run(run_id: str, on_error: str | None = None) -> None:
    """Execute a run's persisted script against real entities.

    Raises :class:`ExecuteRefusedError`/:class:`CapExceededError`/:class:`RuntimeError`.
    """
    policy = _resolve_on_error(on_error)
    runtime = build_runtime()
    manifest = runtime.backup_store.load_manifest(run_id)
    script = (RUNS_PATH / run_id / "script.py").read_text(encoding="utf-8")

    use_case = ExecuteScriptUseCase(
        entity_api=runtime.entity_api,
        relationship_api=runtime.relationship_api,
        entity_repository=runtime.entity_repository,
        backup_store=runtime.backup_store,
        audit_log=runtime.audit_log,
        cap=MAX_ENTITIES_PER_RUN,
        revert_use_case=runtime.revert_use_case,
        file_repository=runtime.file_repository,
    )

    await use_case.execute(
        script,
        manifest,
        run_id=run_id,
        language=DUMMY_LANGUAGE,
        on_error_policy=policy,
    )


async def revert_run(run_id: str) -> None:
    """Revert a run and verify the restore.

    Raises :class:`RevertRefusedError` on refusal.
    """
    runtime = build_runtime()
    await runtime.revert_use_case.revert(run_id)
    await runtime.verify_use_case.verify(run_id)


def delete_run(run_id: str) -> None:
    """Remove a run's entire folder from the backup store."""
    build_backup_store().delete_run(run_id)


def _resolve_on_error(on_error: str | None) -> OnErrorPolicy:
    raw = on_error if on_error is not None else DEFAULT_ON_ERROR_POLICY
    return OnErrorPolicy(raw)


__all__ = [
    "RunDetail",
    "RunSummary",
    "create_and_generate",
    "delete_run",
    "execute_run",
    "get_run",
    "list_runs",
    "revert_run",
]
