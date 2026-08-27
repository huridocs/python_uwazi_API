"""``uwazi-admin-agent revert`` — the revert step (§2.3, §2.6, Phase 5 driver; Phase 6 verification).

Loads the persisted manifest (populated by ``execute``), builds the live
:class:`Runtime`, and runs :class:`RevertRunUseCase` — the pure
:func:`build_revert_actions` ordering (relationships → modified → deleted →
delete-created-last) is executed via ``save_raw``/``delete_by_shared_id``. The
use case sets ``REVERTED`` on the manifest and audits every restore action.

Phase 6 runs :class:`VerifyRevertUseCase` after the revert and folds the
verification result into the exit code (0 only if reverted AND verified ok).
Run ``uwazi-admin-agent verify`` to re-check on demand.

Not unit-tested (needs a real Uwazi instance); the revert *builder* is covered
by ``test_revert.py`` and the use case by ``test_revert_run_use_case.py``.
"""

from __future__ import annotations

import asyncio

from loguru import logger

from uwazi_admin_agent.domain.revert_gate import RevertRefusedError
from uwazi_admin_agent.drivers.runtime import build_runtime


def run_revert(run_name: str) -> int:
    """Revert ``run_name`` and verify the restore; return an exit code."""
    return asyncio.run(_run_revert_async(run_name))


async def _run_revert_async(run_name: str) -> int:
    runtime = build_runtime()
    use_case = runtime.revert_use_case  # already wired with the audit log

    logger.info("revert: run={}", run_name)
    try:
        await use_case.revert(run_name)
    except RevertRefusedError as exc:
        print(f"revert: run={run_name} refused: {exc}", flush=True)
        return 1

    manifest = runtime.backup_store.load_manifest(run_name)
    print(f"revert: run={run_name} status={manifest.status.value}")
    print(
        f"  modified={len(manifest.modified)} deleted={len(manifest.deleted)} "
        f"created={len(manifest.created)} rewired={len(manifest.rewired)}"
    )

    reverted = manifest.status.value == "reverted"
    if not reverted:
        return 1

    verification = await runtime.verify_use_case.verify(run_name)
    print(
        f"  verify: ok={verification.ok} checked={verification.checked} "
        f"mismatches={len(verification.mismatches)} file_gaps={len(verification.file_gaps)} "
        f"relationship_gaps={len(verification.relationship_gaps)}"
    )
    for m in verification.mismatches:
        print(f"    - {m.shared_id} ({m.kind}): expected={m.expected!r} actual={m.actual!r}")
    for g in verification.file_gaps:
        print(f"    - {g.shared_id} (file {g.gap}): {g.kind} {g.originalname!r}")
    for g in verification.relationship_gaps:
        print(f"    - {g.shared_id} (relationship {g.gap}): {g.from_shared_id} -> {g.to_shared_id} type={g.relation_type}")
    return 0 if verification.ok else 1
