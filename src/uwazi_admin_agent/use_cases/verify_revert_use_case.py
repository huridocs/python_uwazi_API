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
            "verify run={} ok={} checked={} mismatches={} file_gaps={}",
            run_id,
            result.ok,
            result.checked,
            len(result.mismatches),
            len(result.file_gaps),
        )
        return result

    async def _fetch_current_raws(self, manifest: Any) -> dict[str, dict[str, Any] | None]:
        """Fetch the current raw for every checked entity (``None`` if absent).

        Modified/created entries are fetched by their manifest ``shared_id``. A
        **deleted** entry is fetched by its recorded ``restored_shared_id`` (the
        new id Uwazi minted on re-create), falling back to the old ``shared_id``
        when no re-create happened yet (e.g. an older manifest or pre-revert); the
        result is still keyed by the manifest ``shared_id`` so :func:`verify_revert`
        can look it up uniformly.
        """
        fetch_keys: list[tuple[str, str]] = []
        for entry in manifest.modified:
            fetch_keys.append((entry.shared_id, entry.shared_id))
        for entry in manifest.deleted:
            target = entry.restored_shared_id or entry.shared_id
            fetch_keys.append((entry.shared_id, target))
        for entry in manifest.created:
            fetch_keys.append((entry.shared_id, entry.shared_id))
        current_raws: dict[str, dict[str, Any] | None] = {}
        for key, target in fetch_keys:
            current_raws[key] = await self._fetch_optional(target)
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
