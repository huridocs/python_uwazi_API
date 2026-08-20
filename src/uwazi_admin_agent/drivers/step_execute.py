"""``uwazi-admin-agent execute`` — the execute step (§2.2, §2.4, Phase 5 driver; Phase 6 cap + on-error).

Loads the persisted manifest (created by ``generate``) and the emitted script,
builds the live :class:`Runtime`, runs :class:`ExecuteScriptUseCase` so the
script runs against real entities with backup-intercepted CRUD (every
modification snapshots the raw before-state into the backup store + manifest
before applying), and prints the touch-set counts. The use case persists the
populated manifest and sets ``EXECUTED``/``FAILED``.

Phase 6 adds the max-entities cap (enforced mid-script by the intercept — a
:class:`CapExceededError` surfaces here as a clear message) and the
``--on-error`` policy (``stop`` leaves the partial run; ``stop-and-revert``
auto-reverts whatever was backed up before the error).

Not unit-tested (needs a real Uwazi instance); validated via the simulation run.
"""

from __future__ import annotations

import asyncio

from loguru import logger

from uwazi_admin_agent.adapters.script_emitter import SCRIPT_FILENAME
from uwazi_admin_agent.configuration import DEFAULT_ON_ERROR_POLICY, DUMMY_LANGUAGE, MAX_ENTITIES_PER_RUN, RUNS_PATH
from uwazi_admin_agent.domain.cap_enforcement import CapExceededError
from uwazi_admin_agent.domain.execute_gate import ExecuteRefusedError
from uwazi_admin_agent.domain.on_error_policy import OnErrorPolicy
from uwazi_admin_agent.drivers.runtime import build_runtime
from uwazi_admin_agent.use_cases.execute_script_use_case import ExecuteScriptUseCase


def run_execute(run_name: str, on_error: str | None = None) -> int:
    """Execute the validated script against real entities; return an exit code."""
    return asyncio.run(_run_execute_async(run_name, on_error))


async def _run_execute_async(run_name: str, on_error: str | None) -> int:
    script_path = RUNS_PATH / run_name / SCRIPT_FILENAME
    if not script_path.is_file():
        print(f"error: no generated script at {script_path} (run `generate` first)", flush=True)
        return 2

    policy = _resolve_on_error(on_error)
    runtime = build_runtime()
    manifest = runtime.backup_store.load_manifest(run_name)
    script = script_path.read_text(encoding="utf-8")

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

    logger.info("execute: run={} on_error={}", run_name, policy.value)
    try:
        result = await use_case.execute(script, manifest, run_id=run_name, language=DUMMY_LANGUAGE, on_error_policy=policy)
    except ExecuteRefusedError as exc:
        print(f"execute: run={run_name} refused: {exc}", flush=True)
        return 1
    except CapExceededError as exc:
        print(f"execute: run={run_name} status=failed (cap exceeded): {exc}", flush=True)
        return 1
    except RuntimeError as exc:
        print(f"execute: run={run_name} status=failed: {exc}", flush=True)
        return 1

    print(f"execute: run={run_name} status={result.status.value}")
    print(
        f"  modified={len(result.modified)} deleted={len(result.deleted)} "
        f"created={len(result.created)} rewired={len(result.rewired)}"
    )
    return 0 if result.status.value == "executed" else 1


def _resolve_on_error(on_error: str | None) -> OnErrorPolicy:
    """Resolve the policy from the CLI flag or the configured default."""
    raw = on_error if on_error is not None else DEFAULT_ON_ERROR_POLICY
    try:
        return OnErrorPolicy(raw)
    except ValueError:
        raise SystemExit(f"error: invalid --on-error value {raw!r}; expected 'stop' or 'stop-and-revert'") from None
