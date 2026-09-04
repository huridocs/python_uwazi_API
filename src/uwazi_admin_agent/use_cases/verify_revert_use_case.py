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

File-delete restores are verified by CONTENT, not identity: Uwazi mints a
fresh ``_id`` + storage filename on every re-upload, so raw equality cannot
see a restored file. Two checks cover them:

- a re-created entity (same-run entity delete + file deletes) is covered by
  the deleted-entity check, whose expected file set now merges the entity's
  run-deleted records into the snapshot's captured files (see
  :func:`verify_revert`);
- a STILL-EXISTING entity's deleted files are verified by fetching each
  current file's bytes and each record's captured bytes, and checking every
  expected ``(kind, originalname, sha256)`` signature is present
  (:func:`build_deleted_file_gaps` — a containment check: reverting a dedupe
  cleanup intentionally re-creates duplicate copies, so extras are not gaps).
  This needs :class:`FileRepositoryPort` byte fetches and the backup store's
  captured bytes, so the USE CASE computes the signatures and hands the gaps
  to the pure decision. An unwired file port with recorded file deletes is a
  loud error, not a silent skip.

Verification is **invalidate-then-refetch**: the optional ``cache_control``
port (the runtime wires the shared cache) drops every cached raw the checks
are about to fetch BEFORE fetching them — verification correctness
outweighs cache hits (the same rationale ``ENTITY_CACHE_FRESH_SNAPSHOTS``
documents for backups), so verification can never read through an entry our
own writes could have made stale. The pure :func:`verify_revert` decision
stays pure; only the fetches change.

Thin I/O orchestrator over the pure decisions; tested with the in-memory
repo + store pattern.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from uwazi_admin_agent.domain.file_dedupe import file_digest
from uwazi_admin_agent.domain.file_restore import extract_file_refs
from uwazi_admin_agent.domain.relationship_restore import extract_inbound_refs_from_existing
from uwazi_admin_agent.domain.revert_verification import (
    FileContentSignature,
    RevertVerificationResult,
    build_deleted_file_gaps,
    verify_revert,
)
from uwazi_admin_agent.ports.backup_store_port import BackupStorePort
from uwazi_admin_agent.ports.cache_invalidation_port import CacheInvalidationPort
from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort
from uwazi_admin_agent.ports.file_repository_port import FileRepositoryPort


class VerifyRevertUseCase:
    """Verify a run's revert by fetching current raws and comparing to snapshots."""

    def __init__(
        self,
        entity_repository: EntityRepositoryPort,
        backup_store: BackupStorePort,
        file_repository: FileRepositoryPort | None = None,
        cache_control: CacheInvalidationPort | None = None,
    ) -> None:
        self._entity_repository: EntityRepositoryPort = entity_repository
        self._backup_store: BackupStorePort = backup_store
        self._file_repository: FileRepositoryPort | None = file_repository
        self._cache_control: CacheInvalidationPort | None = cache_control

    async def verify(self, run_id: str) -> RevertVerificationResult:
        """Load manifest + snapshots, fetch current raws, return the verification result."""
        manifest = self._backup_store.load_manifest(run_id)
        snapshots = {
            sid: self._backup_store.load_snapshot(run_id, sid)
            for sid in {e.shared_id for e in (*manifest.modified, *manifest.deleted)}
        }
        fetch_keys = self._build_fetch_keys(manifest, snapshots)
        self._invalidate_checks(fetch_keys, manifest)  # invalidate-then-refetch
        current_raws = await self._fetch_current_raws(fetch_keys)
        deleted_file_gaps = await self._deleted_file_gaps(manifest, run_id)
        result = verify_revert(manifest, snapshots, current_raws, deleted_file_gaps)
        logger.info(
            "verify run={} ok={} checked={} mismatches={} file_gaps={} relationship_gaps={}",
            run_id,
            result.ok,
            result.checked,
            len(result.mismatches),
            len(result.file_gaps),
            len(result.relationship_gaps),
        )
        return result

    async def _deleted_file_gaps(self, manifest: Any, run_id: str) -> list[Any]:
        """Content-verify the deleted-file restores on STILL-EXISTING entities.

        Entities the same run also deleted are covered by the deleted-entity
        check inside :func:`verify_revert` (their re-created raw is compared
        against snapshot files + records by name+kind); this pass covers the
        rest — entities whose only change was file deletes, so no snapshot
        exists. Per entity: expected signatures from the captured bytes
        (sha256), actual signatures from the current raw's fetched bytes, and
        the pure containment decision. Best-effort per byte fetch (a ``None``
        becomes a wildcard digest — no false gaps).
        """
        if not manifest.deleted_files:
            return []
        if self._file_repository is None:
            raise RuntimeError(
                "verify cannot check deleted-file restores without a wired file_repository (got None). "
                "Wire FileRepositoryPort into VerifyRevertUseCase (the runtime always does)."
            )
        deleted_entities = {e.shared_id for e in manifest.deleted}
        gaps: list[Any] = []
        for shared_id in _distinct_file_entity_ids(manifest):
            if shared_id in deleted_entities:
                continue  # covered by the deleted-entity check in verify_revert
            gaps.extend(await self._entity_file_gaps(manifest, run_id, shared_id))
        return gaps

    async def _entity_file_gaps(self, manifest: Any, run_id: str, shared_id: str) -> list[Any]:
        """One still-existing entity's deleted-file restore gaps (containment)."""
        records = [r for r in manifest.deleted_files if r.shared_id == shared_id]
        expected = [await self._expected_signature(record, run_id) for record in records]
        actual = await self._actual_signatures(shared_id)
        return build_deleted_file_gaps(shared_id, expected, actual)

    async def _expected_signature(self, record: Any, run_id: str) -> FileContentSignature:
        """One record's content signature from its captured bytes (wildcard on load failure)."""
        try:
            data = self._backup_store.load_file_bytes(run_id, record.shared_id, record.file_id)
        except Exception:  # noqa: BLE001 — best-effort: unknown bytes degrade to a wildcard digest
            return (record.kind, record.originalname, None)
        return (record.kind, record.originalname, file_digest(data))

    async def _actual_signatures(self, shared_id: str) -> list[FileContentSignature]:
        """The entity's CURRENT uploaded files' content signatures (wildcard on fetch failure)."""
        assert self._file_repository is not None  # guarded by _deleted_file_gaps
        raw = await self._fetch_optional(shared_id)
        if raw is None:
            return []  # the entity vanished post-revert: every expected file is a gap
        signatures: list[FileContentSignature] = []
        for ref in extract_file_refs(raw):
            data = await self._file_repository.get_file_bytes(ref.filename)
            digest = file_digest(data) if data is not None else None
            signatures.append((ref.kind, ref.originalname, digest))
        return signatures

    def _invalidate_checks(self, fetch_keys: list[tuple[str, str]], manifest: Any) -> None:
        """Drop every cached raw the checks are about to fetch (no-op without a cache port).

        Belt-and-suspenders freshness: our writes invalidate as they happen,
        but verification must never READ THROUGH an entry our own writes
        could have made stale. Includes the still-existing entities carrying
        deleted-file records (the content checks fetch those raws too, and
        they are not part of the manifest's entity lists).
        """
        if self._cache_control is None:
            return
        ids = {target for _key, target in fetch_keys}
        ids.update(_distinct_file_entity_ids(manifest))
        if ids:
            self._cache_control.invalidate_entities(sorted(ids))

    async def _fetch_current_raws(self, fetch_keys: list[tuple[str, str]]) -> dict[str, dict[str, Any] | None]:
        """Fetch the current raw for every fetch key (``None`` if absent)."""
        current_raws: dict[str, dict[str, Any] | None] = {}
        for key, target in fetch_keys:
            current_raws[key] = await self._fetch_optional(target)
        return current_raws

    def _build_fetch_keys(self, manifest: Any, snapshots: dict[str, Any]) -> list[tuple[str, str]]:
        """The (result key, fetch target) pairs verification will read.

        Modified/created entries are fetched by their manifest ``shared_id``. A
        **deleted** entry is fetched by its recorded ``restored_shared_id`` (the
        new id Uwazi minted on re-create), falling back to the old ``shared_id``
        when no re-create happened yet (e.g. an older manifest or pre-revert); the
        result is still keyed by the manifest ``shared_id`` so :func:`verify_revert`
        can look it up uniformly.

        Still-existing entities that held an inbound ref to a deleted entity
        (cascade-stripped on delete; restored by revert's inbound-ref re-apply)
        are NOT in the manifest, so they are discovered here from the deleted
        snapshots' ``relations`` and fetched by their own sharedId so the
        inbound-ref gap check can inspect their post-revert relations. Existing
        entities that are themselves manifest members are excluded (documented
        stale-id limitation).
        """
        fetch_keys: list[tuple[str, str]] = []
        for entry in manifest.modified:
            fetch_keys.append((entry.shared_id, entry.shared_id))
        for entry in manifest.deleted:
            target = entry.restored_shared_id or entry.shared_id
            fetch_keys.append((entry.shared_id, target))
        for entry in manifest.created:
            fetch_keys.append((entry.shared_id, entry.shared_id))

        deleted_ids = {e.shared_id for e in manifest.deleted}
        deleted_snapshots = {sid: snapshots[sid] for sid in deleted_ids if sid in snapshots}
        manifest_ids = (
            {e.shared_id for e in manifest.modified}
            | {e.shared_id for e in manifest.deleted}
            | {e.shared_id for e in manifest.created}
        )
        inbound_refs = extract_inbound_refs_from_existing(deleted_snapshots, deleted_ids, excluded_existing=manifest_ids)
        for ref in inbound_refs:
            fetch_keys.append((ref.existing_shared_id, ref.existing_shared_id))

        return fetch_keys

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


def _distinct_file_entity_ids(manifest: Any) -> list[str]:
    """The distinct entities carrying deleted-file records, first-appearance order."""
    seen: set[str] = set()
    ordered: list[str] = []
    for record in manifest.deleted_files:
        if record.shared_id not in seen:
            seen.add(record.shared_id)
            ordered.append(record.shared_id)
    return ordered
