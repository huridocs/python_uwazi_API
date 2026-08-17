"""``uwazi-admin-agent revert`` — the revert step (§2.3, §2.6, Phase 5 driver).

Loads the persisted manifest (populated by ``execute``), builds the live
:class:`Runtime`, and runs :class:`RevertRunUseCase` — the pure
:func:`build_revert_actions` ordering (relationships → modified → deleted →
delete-created-last) is executed via ``save_raw``/``delete_by_shared_id``. The
use case sets ``REVERTED`` on the manifest.

Not unit-tested (needs a real Uwazi instance); the revert *builder* is covered
by ``test_revert.py`` and the use case by ``test_revert_run_use_case.py``.
"""

from __future__ import annotations

import asyncio

from loguru import logger

from uwazi_admin_agent.drivers.runtime import build_runtime
from uwazi_admin_agent.use_cases.revert_run_use_case import RevertRunUseCase


def run_revert(run_name: str) -> int:
    """Revert ``run_name`` by restoring backed-up entities; return an exit code."""
    return asyncio.run(_run_revert_async(run_name))


async def _run_revert_async(run_name: str) -> int:
    runtime = build_runtime()
    use_case = RevertRunUseCase(
        entity_repository=runtime.entity_repository,
        backup_store=runtime.backup_store,
    )

    logger.info("revert: run={}", run_name)
    await use_case.revert(run_name)

    manifest = runtime.backup_store.load_manifest(run_name)
    print(f"revert: run={run_name} status={manifest.status.value}")
    print(
        f"  modified={len(manifest.modified)} deleted={len(manifest.deleted)} "
        f"created={len(manifest.created)} rewired={len(manifest.rewired)}"
    )
    return 0 if manifest.status.value == "reverted" else 1
