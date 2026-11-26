"""Web driver module split out of the original ``run_service`` (behavior preserved)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import yaml

from uwazi_admin_agent.adapters.audit_log_adapter import JsonlAuditLog
from uwazi_admin_agent.domain.audit_record import AuditRecord, AuditStep
from uwazi_admin_agent.domain.manifest import MigrationManifest
from uwazi_admin_agent.domain.run_name import validate_run_name
from uwazi_admin_agent.domain.task_export import tasks_csv
from uwazi_admin_agent.drivers.runtime import build_audit_log, build_backup_store
from uwazi_admin_agent.ports.audit_log_port import AuditLogPort
from uwazi_admin_agent.ports.backup_store_port import BackupStorePort

from .models import ExecutionEvent, RunDetail, RunResults, RunSummary
from .wiring import _paths, build_suggestion_store


def _touch_counts(manifest: MigrationManifest) -> tuple[int, int, int, int]:
    """(modified, deleted, created, rewired) counts still outstanding in Uwazi.

    Delegates to the manifest so the partial-revert subtraction (and the
    full-revert zeroing) lives with the touch-set data it describes.
    """
    return manifest.touch_counts()


def list_runs(url: str) -> list[RunSummary]:
    """Return all persisted runs (skips folders whose manifest is unreadable).

    Order is the TABLE's job: every summary carries ``created_at``, and the runs
    table sorts by it (newest first) so the task just created sits at the top.
    No sort here: a caller that wants another order (an export, a name list)
    gets the store's own listing rather than a second, hidden policy.
    """
    store = build_backup_store(url)
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


def get_run(url: str, run_id: str) -> RunDetail:
    """Return a run's manifest fields + the emitted script source."""
    run_id = validate_run_name(run_id)
    store = build_backup_store(url)
    manifest = store.load_manifest(run_id)
    script_path = _paths(url).runs_path / run_id / "script.py"
    script = script_path.read_text(encoding="utf-8") if script_path.is_file() else manifest.script
    modified, deleted, created, rewired = _touch_counts(manifest)
    return RunDetail(
        run_id=manifest.run_id,
        status=manifest.status,
        created_at=manifest.created_at,
        prompt=manifest.prompt,
        validated_prompt=manifest.validated_prompt,
        script=script,
        modified=modified,
        deleted=deleted,
        created=created,
        rewired=rewired,
        error=manifest.error,
        plan_kind=manifest.plan_kind,
        plan_description=manifest.plan_description,
        llm_budget=manifest.llm_budget,
        validation_config=manifest.validation_config,
    )


def export_tasks_csv(url: str) -> str:
    """Every run of ONE instance as CSV text, prompts included.

    Reads each manifest once (the same store the table reads) so the export is
    a snapshot taken at the click, not a live join: a run deleted mid-export
    cannot produce a half row. An unreadable manifest is skipped, matching
    :func:`list_runs`.
    """
    store = build_backup_store(url)
    rows: list[dict[str, Any]] = []
    for run_id in store.list_runs():
        try:
            manifest = store.load_manifest(run_id)
        except FileNotFoundError:
            continue
        modified, deleted, created, rewired = _touch_counts(manifest)
        rows.append(
            {
                "run_id": manifest.run_id,
                "status": manifest.status.value,
                "created_at": manifest.created_at,
                "last_executed_at": manifest.last_executed_at,
                "modified": modified,
                "deleted": deleted,
                "created": created,
                "rewired": rewired,
                "prompt": manifest.prompt,
                "validated_prompt": manifest.validated_prompt,
                "error": manifest.error,
            }
        )
    return tasks_csv(rows)


def get_run_audit(url: str, run_id: str) -> list[AuditRecord]:
    """Return a run's full audit trail (empty when the run has no audit log)."""
    return build_audit_log(url).load(validate_run_name(run_id))


def get_run_results(url: str, run_id: str) -> RunResults:
    """Return a run's touch-set (the entities/files it changed).

    Reads the manifest directly (not the count-reduced :func:`get_run`) so a
    reverted run still shows its full touch-set — the manifest keeps the lists
    for post-revert verification even though the table counts read as 0.

    Also resolves each touched entity's title with zero HTTP calls: recorded
    applied writes carry the title the script set, and anything still missing
    (a legacy run that recorded no writes, or an entity only touched by a
    rewire/delete) is read out of that entity's on-disk before-snapshot. A
    missing snapshot is skipped — the UI falls back to the shared id.
    """
    store = build_backup_store(url)
    manifest = store.load_manifest(run_id)
    return RunResults(
        run_id=manifest.run_id,
        status=manifest.status,
        result=manifest.result,
        modified=manifest.modified,
        created=manifest.created,
        deleted=manifest.deleted,
        rewired=manifest.rewired,
        deleted_files=manifest.deleted_files,
        applied_writes=manifest.applied_writes,
        titles=_resolve_titles(store, run_id, manifest),
    )


def _resolve_titles(store: BackupStorePort, run_id: str, manifest: MigrationManifest) -> dict[str, str]:
    """Build the shared_id → title map for a run, from run data only (no HTTP).

    Two sources, in order: the titles the applied writes recorded (what the
    script set, always current), then the raw ``title`` inside each touched
    entity's before-snapshot (covers legacy runs and entities this run only
    rewired or deleted). Entities created by this run have no snapshot — their
    title comes from the create's applied write.
    """
    titles: dict[str, str] = {}
    for write in manifest.applied_writes:
        if write.title:
            titles[write.shared_id] = write.title
    created_ids = {e.shared_id for e in manifest.created}
    touched = [e.shared_id for e in (*manifest.modified, *manifest.created, *manifest.deleted)]
    for shared_id in touched:
        if shared_id in titles or shared_id in created_ids:
            continue
        try:
            snapshot = store.load_snapshot(run_id, shared_id)
        except Exception:  # noqa: BLE001 — a missing/broken snapshot must not break the dialog
            continue
        title = (snapshot.raw or {}).get("title")
        if isinstance(title, str) and title:
            titles[shared_id] = title
    return titles


def _run_level_events(records: list[AuditRecord]) -> list[AuditRecord]:
    """Keep only run-level lifecycle audit records (execute / revert).

    Per-entity writes (update, delete, restore_*, …) carry ``shared_ids`` and are
    excluded; the run-level records emitted by the execute/revert use cases use
    ``op_kind`` ``"execute"`` / ``"revert"`` with empty ``shared_ids``.
    """
    return [r for r in records if r.op_kind in ("execute", "revert")]


def get_execution_history(url: str, run_id: str, audit_log: AuditLogPort | None = None) -> list[ExecutionEvent]:
    """Return a run's execute/revert events, newest first, for the history modal.

    Reads the run's on-disk audit log (``<runs>/<run_id>/audit.jsonl``) unless
    an ``audit_log`` is injected (used by tests); a run with no audit log (never
    executed, or pre-Phase 6) yields an empty list.
    """
    log = audit_log if audit_log is not None else JsonlAuditLog(_paths(url).runs_path)
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


def get_table_update_time(url: str, run_id: str) -> datetime | None:
    """When the table was last refreshed from Uwazi (None = never)."""
    records = build_audit_log(url).load(run_id)
    timestamps = [record.timestamp for record in records if record.step is AuditStep.TABLE_SYNC]
    return max(timestamps) if timestamps else None


def delete_run(url: str, run_id: str) -> None:
    """Remove a run's entire folder from the backup store + its suggestion rows."""
    run_id = validate_run_name(run_id)
    build_backup_store(url).delete_run(run_id)
    # The suggestion DB is one shared file, so a run's rows do not disappear
    # with its folder: they are deleted explicitly here.
    build_suggestion_store(url).delete_run(run_id)


def rename_run(url: str, old_id: str, new_id: str) -> None:
    """Rename a run end-to-end.

    Moves the run folder + manifest (via the backup store), relocates its prompt
    YAML under the instance's ``prompts/`` dir, and repoints ``active_run.yaml`` if it targets
    the old id. Both ids are validated by :func:`validate_run_name` (no path
    separators or ``..``); the store raises if the target already exists.
    """
    old_id = validate_run_name(old_id)
    new_id = validate_run_name(new_id)
    # Relocate the prompt YAML if one exists for the old id.
    old_prompt = _paths(url).prompts_path / f"{old_id}.yaml"
    new_prompt = _paths(url).prompts_path / f"{new_id}.yaml"
    if old_prompt.is_file():
        old_prompt.rename(new_prompt)
    # Repoint the active-run pointer if it targets the old id.
    if _paths(url).runs_file.is_file():
        data = yaml.safe_load(_paths(url).runs_file.read_text(encoding="utf-8")) or {}
        if isinstance(data, dict) and data.get("active_run") == old_id:
            data["active_run"] = new_id
            _paths(url).runs_file.write_text(yaml.dump(data), encoding="utf-8")
    build_backup_store(url).rename_run(old_id, new_id)
    # The review table, the prediction pass and drift rows live in the shared
    # SQLite database keyed by run_id — renaming only the folder left the
    # renamed run's review page empty and the old id's rows orphaned.
    build_suggestion_store(url).rename_run(old_id, new_id)
