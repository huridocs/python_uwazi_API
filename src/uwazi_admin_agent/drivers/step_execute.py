"""``uwazi-admin-agent execute`` — the execute step (§2.2, §2.4, Phase 5 driver).

Loads the persisted manifest (created by ``generate``) and the emitted script,
builds the live :class:`Runtime`, runs :class:`ExecuteScriptUseCase` so the
script runs against real entities with backup-intercepted CRUD (every
modification snapshots the raw before-state into the backup store + manifest
before applying), and prints the touch-set counts. The use case persists the
populated manifest and sets ``EXECUTED``/``FAILED``.

Not unit-tested (needs a real Uwazi instance); validated via the simulation run.
"""

from __future__ import annotations

import asyncio

from loguru import logger

from uwazi_admin_agent.adapters.script_emitter import SCRIPT_FILENAME
from uwazi_admin_agent.configuration import DUMMY_LANGUAGE, RUNS_PATH
from uwazi_admin_agent.drivers.runtime import build_runtime
from uwazi_admin_agent.use_cases.execute_script_use_case import ExecuteScriptUseCase


def run_execute(run_name: str) -> int:
    """Execute the validated script against real entities; return an exit code."""
    return asyncio.run(_run_execute_async(run_name))


async def _run_execute_async(run_name: str) -> int:
    script_path = RUNS_PATH / run_name / SCRIPT_FILENAME
    if not script_path.is_file():
        print(f"error: no generated script at {script_path} (run `generate` first)", flush=True)
        return 2

    runtime = build_runtime()
    manifest = runtime.backup_store.load_manifest(run_name)
    script = script_path.read_text(encoding="utf-8")

    use_case = ExecuteScriptUseCase(
        entity_api=runtime.entity_api,
        relationship_api=runtime.relationship_api,
        entity_repository=runtime.entity_repository,
        backup_store=runtime.backup_store,
    )

    logger.info("execute: run={}", run_name)
    result = await use_case.execute(script, manifest, run_id=run_name, language=DUMMY_LANGUAGE)

    print(f"execute: run={run_name} status={result.status.value}")
    print(
        f"  modified={len(result.modified)} deleted={len(result.deleted)} "
        f"created={len(result.created)} rewired={len(result.rewired)}"
    )
    return 0 if result.status.value == "executed" else 1
