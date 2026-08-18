"""Post-revert verification use case (§5 Phase 6).

After :class:`RevertRunUseCase` restores, the operator must be able to confirm
the restore actually matched the snapshots — not just trust the revert. This
use case loads the persisted manifest + snapshots, fetches the *current* raws
from the entity repository, and delegates to the pure
:func:`uwazi_admin_agent.domain.revert_verification.verify_revert` for the
decision. The CLI's ``verify`` subcommand and the ``revert`` step's
post-revert check both run this.

For modified/deleted entities the current raw is fetched (a not-found entity
maps to ``None`` — a deleted entity that failed to re-create is a mismatch).
For created entities the current raw is fetched (``None`` = gone, the expected
post-revert state; a present raw = mismatch, the created entity survived).

Thin I/O orchestrator over the pure decision; tested with the in-memory
repo + store pattern.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from uwazi_admin_agent.domain.revert_verification import RevertVerificationResult, verify_revert
from uwazi_admin_agent.ports.backup_store_port import BackupStorePort
from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort


class VerifyRevertUseCase:
    """Verify a run's revert by fetching current raws and comparing to snapshots."""

    def __init__(
        self,
        entity_repository: EntityRepositoryPort,
        backup_store: BackupStorePort,
    ) -> None:
        self._entity_repository: EntityRepositoryPort = entity_repository
        self._backup_store: BackupStorePort = backup_store

    async def verify(self, run_id: str) -> RevertVerificationResult:
        """Load manifest + snapshots, fetch current raws, return the verification result."""
        manifest = self._backup_store.load_manifest(run_id)
        snapshots = {
            sid: self._backup_store.load_snapshot(run_id, sid)
            for sid in {e.shared_id for e in (*manifest.modified, *manifest.deleted)}
        }
        current_raws = await self._fetch_current_raws(manifest)
        result = verify_revert(manifest, snapshots, current_raws)
        logger.info(
            "verify run={} ok={} checked={} mismatches={}",
            run_id,
            result.ok,
            result.checked,
            len(result.mismatches),
        )
        return result

    async def _fetch_current_raws(self, manifest: Any) -> dict[str, dict[str, Any] | None]:
        """Fetch the current raw for every checked entity (``None`` if absent)."""
        checked_ids: set[str] = {e.shared_id for e in (*manifest.modified, *manifest.deleted, *manifest.created)}
        current_raws: dict[str, dict[str, Any] | None] = {}
        for sid in checked_ids:
            current_raws[sid] = await self._fetch_optional(sid)
        return current_raws

    async def _fetch_optional(self, shared_id: str) -> dict[str, Any] | None:
        """Fetch a raw by sharedId; return ``None`` if the entity is absent (deleted)."""
        try:
            return await self._entity_repository.get_raw_by_shared_id(shared_id)
        except Exception:
            # A not-found entity is expected for created (revert deleted them) and
            # for a deleted-entity whose re-create failed (a mismatch the verifier
            # reports). Any other fetch error is also surfaced as "absent" so the
            # verifier flags it rather than crashing the run.
            return None
