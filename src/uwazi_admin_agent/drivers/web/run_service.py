"""Async service layer for the admin agent web UI (Phase 5 driver).

Returns data (not prints) so NiceGUI's event loop can call mutating operations
in background tasks. Calls use cases directly — the step drivers
(``step_generate`` etc.) wrap each in ``asyncio.run()``, which is incompatible
with NiceGUI's already-running loop, so this module mirrors their wiring
without the ``asyncio.run`` wrapper.

No business logic: this is a driver that wires adapters to use cases, matching
the ``drivers/`` layer convention.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

import yaml

from uwazi_admin_agent.adapters.audit_log_adapter import JsonlAuditLog
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
from uwazi_admin_agent.domain.audit_record import AuditRecord
from uwazi_admin_agent.domain.manifest import MigrationManifest, RunStatus
from uwazi_admin_agent.domain.on_error_policy import OnErrorPolicy
from uwazi_admin_agent.domain.revert_verification import format_verification_result
from uwazi_admin_agent.drivers.runtime import build_audit_log, build_backup_store, build_runtime
from uwazi_admin_agent.ports.audit_log_port import AuditLogPort
from uwazi_admin_agent.use_cases.execute_script_use_case import ExecuteScriptUseCase
from uwazi_admin_agent.use_cases.generate_script_use_case import GenerateScriptUseCase


class GenerateError(Exception):
    """Generation failed; the manifest is persisted as GENERATION_FAILED."""


class RevertVerificationError(Exception):
    """Revert succeeded but post-revert verification found mismatches."""


@dataclass(frozen=True)
class RunSummary:
    """One row in the runs table — the manifest's headline fields."""

    run_id: str
    last_executed_at: datetime | None
    status: RunStatus
    created_at: datetime
    prompt: str
    modified: int
    deleted: int
    created: int
    rewired: int
    error: str | None


@dataclass(frozen=True)
class RunDetail:
    run_id: str
    status: RunStatus
    created_at: datetime
    prompt: str
    script: str
    modified: int
    deleted: int
    created: int
    rewired: int
    error: str | None


@dataclass(frozen=True)
class ExecutionEvent:
    """One row in a run's execution-history modal (derived from audit records)."""

    timestamp: datetime
    type: str  # "execute" | "revert"
    outcome: str  # "success" | "failure"
    detail: str | None


def _touch_counts(manifest: MigrationManifest) -> tuple[int, int, int, int]:
    """(modified, deleted, created, rewired) counts shown in the table.

    A reverted run has no outstanding changes — everything was restored or
    deleted — so its counts read as 0 even though the manifest keeps the
    touch-set for post-revert verification and audit.
    """
    if manifest.status == RunStatus.REVERTED:
        return 0, 0, 0, 0
    return len(manifest.modified), len(manifest.deleted), len(manifest.created), len(manifest.rewired)


def list_runs() -> list[RunSummary]:
    """Return all persisted runs (skips folders whose manifest is unreadable)."""
    store = build_backup_store()
    summaries: list[RunSummary] = []
    for run_id in store.list_runs():
        try:
            manifest = store.load_manifest(run_id)
        except FileNotFoundError:
            continue
        modified, deleted, created, rewired = _touch_counts(manifest)
        summaries.append(
            RunSummary(
                run_id=manifest.run_id,
                status=manifest.status,
                created_at=manifest.created_at,
                last_executed_at=manifest.last_executed_at,
                prompt=manifest.prompt,
                modified=modified,
                deleted=deleted,
                created=created,
                rewired=rewired,
                error=manifest.error,
            )
        )
    return summaries


def get_run(run_id: str) -> RunDetail:
    """Return a run's manifest fields + the emitted script source."""
    store = build_backup_store()
    manifest = store.load_manifest(run_id)
    script_path = RUNS_PATH / run_id / "script.py"
    script = script_path.read_text(encoding="utf-8") if script_path.is_file() else manifest.script
    modified, deleted, created, rewired = _touch_counts(manifest)
    return RunDetail(
        run_id=manifest.run_id,
        status=manifest.status,
        created_at=manifest.created_at,
        prompt=manifest.prompt,
        script=script,
        modified=modified,
        deleted=deleted,
        created=created,
        rewired=rewired,
        error=manifest.error,
    )


def get_run_audit(run_id: str) -> list[AuditRecord]:
    """Return a run's full audit trail (empty when the run has no audit log)."""
    return build_audit_log().load(run_id)


def _run_level_events(records: list[AuditRecord]) -> list[AuditRecord]:
    """Keep only run-level lifecycle audit records (execute / revert).

    Per-entity writes (update, delete, restore_*, …) carry ``shared_ids`` and are
    excluded; the run-level records emitted by the execute/revert use cases use
    ``op_kind`` ``"execute"`` / ``"revert"`` with empty ``shared_ids``.
    """
    return [r for r in records if r.op_kind in ("execute", "revert")]


def get_execution_history(run_id: str, audit_log: AuditLogPort | None = None) -> list[ExecutionEvent]:
    """Return a run's execute/revert events, newest first, for the history modal.

    Reads the run's on-disk audit log (``<RUNS_PATH>/<run_id>/audit.jsonl``) unless
    an ``audit_log`` is injected (used by tests); a run with no audit log (never
    executed, or pre-Phase 6) yields an empty list.
    """
    log = audit_log if audit_log is not None else JsonlAuditLog(RUNS_PATH)
    audit = log.load(run_id)
    events = _run_level_events(audit)
    events.sort(key=lambda r: r.timestamp, reverse=True)
    return [
        ExecutionEvent(
            timestamp=r.timestamp,
            type=r.op_kind,
            outcome=r.outcome.value,
            detail=r.detail,
        )
        for r in events
    ]


async def create_and_generate(name: str, prompt: str, user: str, password: str) -> None:
    """Write the prompt + active-run pointer, then generate the script + manifest.

    On failure the manifest is persisted with ``status=GENERATION_FAILED`` (the
    prompt yaml stays on disk for retry) and :class:`GenerateError` is raised
    so the caller shows a short toast; the full detail lives on the manifest.
    """
    PROMPTS_PATH.mkdir(parents=True, exist_ok=True)
    (PROMPTS_PATH / f"{name}.yaml").write_text(yaml.dump({"prompt": prompt}), encoding="utf-8")
    RUNS_FILE.parent.mkdir(parents=True, exist_ok=True)
    RUNS_FILE.write_text(yaml.dump({"active_run": name}), encoding="utf-8")

    loader = RunsConfigLoader.default()
    run_path = loader.load_active_path()

    runtime = build_runtime(user=user, password=password)
    use_case = GenerateScriptUseCase(
        llm=runtime.llm,
        deps=runtime.deps,
        entity_repository=runtime.entity_repository,
    )

    try:
        script = await use_case.execute(prompt)
        emit_generated_script(script, run_path)
    except Exception as exc:
        manifest = MigrationManifest(
            run_id=name,
            created_at=datetime.now(timezone.utc),
            prompt=prompt,
            script="",
            status=RunStatus.GENERATION_FAILED,
            snapshot_dir=str(run_path),
            error=str(exc),
            error_step="generate",
        )
        runtime.backup_store.save_manifest(name, manifest)
        raise GenerateError(str(exc)) from exc

    manifest = MigrationManifest(
        run_id=name,
        created_at=datetime.now(timezone.utc),
        prompt=prompt,
        script=script.python_code,
        status=RunStatus.PLANNED,
        snapshot_dir=str(run_path),
    )
    runtime.backup_store.save_manifest(name, manifest)


async def execute_run(run_id: str, user: str, password: str, on_error: str | None = None) -> None:
    """Execute a run's persisted script against real entities.

    Raises :class:`GenerateError` when the run never generated a script,
    :class:`ExecuteRefusedError` when the gate refuses, or
    :class:`ScriptExecutionError`/:class:`CapExceededError` on script failure.
    """
    policy = _resolve_on_error(on_error)
    runtime = build_runtime(user=user, password=password)
    manifest = runtime.backup_store.load_manifest(run_id)
    if manifest.status == RunStatus.GENERATION_FAILED:
        raise GenerateError("script generation failed; retry or delete the task")
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


async def revert_run(run_id: str, user: str, password: str) -> None:
    """Revert a run and verify the restore.

    Raises :class:`RevertRefusedError` on refusal and
    :class:`RevertVerificationError` when the revert succeeded but the
    post-revert verification found mismatches (persisted on the manifest).
    """
    runtime = build_runtime(user=user, password=password)
    await runtime.revert_use_case.revert(run_id)
    result = await runtime.verify_use_case.verify(run_id)
    if not result.ok:
        detail = format_verification_result(result)
        manifest = runtime.backup_store.load_manifest(run_id)
        manifest.error = detail
        manifest.error_step = "verify"
        runtime.backup_store.save_manifest(run_id, manifest)
        raise RevertVerificationError(detail)


def delete_run(run_id: str) -> None:
    """Remove a run's entire folder from the backup store."""
    build_backup_store().delete_run(run_id)


def rename_run(old_id: str, new_id: str) -> None:
    """Rename a run end-to-end.

    Moves the run folder + manifest (via the backup store), relocates its prompt
    YAML under ``PROMPTS_PATH``, and repoints ``active_run.yaml`` if it targets
    the old id. ``new_id`` must be a non-empty, filesystem-safe name (no path
    separators or ``..``); the store raises if the target already exists.
    """
    new_id = (new_id or "").strip()
    if not new_id:
        raise ValueError("New run name is required")
    if "/" in new_id or "\\" in new_id or new_id in (".", ".."):
        raise ValueError(f"Invalid run name: {new_id!r}")
    # Relocate the prompt YAML if one exists for the old id.
    old_prompt = PROMPTS_PATH / f"{old_id}.yaml"
    new_prompt = PROMPTS_PATH / f"{new_id}.yaml"
    if old_prompt.is_file():
        old_prompt.rename(new_prompt)
    # Repoint the active-run pointer if it targets the old id.
    if RUNS_FILE.is_file():
        data = yaml.safe_load(RUNS_FILE.read_text(encoding="utf-8")) or {}
        if isinstance(data, dict) and data.get("active_run") == old_id:
            data["active_run"] = new_id
            RUNS_FILE.write_text(yaml.dump(data), encoding="utf-8")
    build_backup_store().rename_run(old_id, new_id)


def _resolve_on_error(on_error: str | None) -> OnErrorPolicy:
    raw = on_error if on_error is not None else DEFAULT_ON_ERROR_POLICY
    return OnErrorPolicy(raw)


__all__ = [
    "RunDetail",
    "RunSummary",
    "create_and_generate",
    "delete_run",
    "rename_run",
    "execute_run",
    "get_run",
    "list_runs",
    "revert_run",
]


__all__ = [
    "GenerateError",
    "RevertVerificationError",
    "RunDetail",
    "RunSummary",
    "create_and_generate",
    "delete_run",
    "rename_run",
    "execute_run",
    "get_run",
    "get_run_audit",
    "list_runs",
    "revert_run",
]
