"""``uwazi-admin-agent verify`` — the standalone post-revert verification step (§5 Phase 6).

Re-checks a reverted run without re-reverting: fetches the current raws for
every entity the run touched (modified, deleted, created) and confirms they
match the snapshots + the recorded before-states, via the pure
:func:`verify_revert`. The manual DoD run uses this to "catch a simulated
mismatch" (edit an entity post-revert, run ``verify``, see the mismatch).

Not unit-tested (needs a real Uwazi instance); the pure decision is covered by
``test_revert_verification.py`` and the use case by ``test_verify_revert_use_case.py``.
"""

from __future__ import annotations

import asyncio

from loguru import logger

from uwazi_admin_agent.drivers.runtime import build_runtime


def run_verify(run_name: str) -> int:
    """Verify a run's revert; return an exit code (0 if ok, 1 if mismatches)."""
    return asyncio.run(_run_verify_async(run_name))


async def _run_verify_async(run_name: str) -> int:
    runtime = build_runtime()
    logger.info("verify: run={}", run_name)
    result = await runtime.verify_use_case.verify(run_name)

    print(
        f"verify: run={run_name} ok={result.ok} checked={result.checked} "
        f"mismatches={len(result.mismatches)} file_gaps={len(result.file_gaps)}"
    )
    for m in result.mismatches:
        print(f"  - {m.shared_id} ({m.kind}): expected={m.expected!r} actual={m.actual!r}")
    for g in result.file_gaps:
        print(f"  - {g.shared_id} (file {g.gap}): {g.kind} {g.originalname!r}")
    return 0 if result.ok else 1
