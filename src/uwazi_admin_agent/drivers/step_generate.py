"""``uwazi-admin-agent generate`` — the generate step (§2.2, Phase 5 driver).

Loads the active :class:`RunConfig`, resolves the run folder, builds the live
:class:`Runtime`, runs :class:`GenerateScriptUseCase` (the agent calls
``run_validation_script`` internally — simulate is folded into generate per
§2.2/Phase 3), emits the script to ``<run_path>/script.py``, and persists an
initial :class:`MigrationManifest` (``status=PLANNED``) so ``inspect-run`` works
before ``execute`` populates it.

Not unit-tested (needs an LLM + a real Uwazi instance); validated via the
simulation run.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from loguru import logger

from uwazi_admin_agent.adapters.runs_config_loader import RunsConfigLoader
from uwazi_admin_agent.adapters.script_emitter import emit_generated_script
from uwazi_admin_agent.domain.manifest import MigrationManifest, RunStatus
from uwazi_admin_agent.drivers.runtime import build_runtime
from uwazi_admin_agent.use_cases.generate_script_use_case import GenerateScriptUseCase


def run_generate() -> int:
    """Run the generate step for the active run; return a process exit code."""
    return asyncio.run(_run_generate_async())


async def _run_generate_async() -> int:
    loader = RunsConfigLoader.default()
    config = loader.load_active()
    run_path = loader.load_active_path()

    runtime = build_runtime()
    use_case = GenerateScriptUseCase(
        llm=runtime.llm,
        deps=runtime.deps,
        entity_repository=runtime.entity_repository,
    )

    logger.info("generate: run={} prompt_tokens~{}", config.name, len(config.prompt) // 4)
    script = await use_case.execute(config.prompt)

    script_path = emit_generated_script(script, run_path)

    manifest = MigrationManifest(
        run_id=config.name,
        created_at=datetime.now(timezone.utc),
        prompt=config.prompt,
        script=script.python_code,
        status=RunStatus.PLANNED,
        snapshot_dir=str(run_path),
    )
    runtime.backup_store.save_manifest(config.name, manifest)

    print(f"generated script: {script_path}")
    print(f"manifest: {run_path / 'manifest.json'} ({manifest.status.value})")
    return 0
