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
own writes could have made stale. The same invalidation runs again AFTER the
fetches (in a ``finally``), because the refetch repopulated those very keys
and verification is an audit, not a cache warm-up. The pure
:func:`verify_revert` decision stays pure; only the fetches change.

**Every fetch carries the recorded ROW language.** A sharedId can have one
entity row per configured language (Uwazi multilingual entities), each with
its own ``_id``, its own ``language`` and its own i18n-TRANSLATED metadata
``label`` values. Fetching without a locale lets the server return whichever
row it picks first (Uwazi's default locale), and comparing, say, a Spanish
snapshot against the English row flags a false mismatch on ``_id``,
``language`` and every translated label — on EVERY multilingual entity in
the run. The language comes from the manifest entry (recorded from the
snapshot at backup time) or, for the non-manifest inbound-ref endpoints, the
hub row's embedded ``entityData.language``.

Thin I/O orchestrator over the pure decisions; tested with the in-memory
repo + store pattern.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from uwazi_admin_agent.domain.file_dedupe import file_digest
from uwazi_admin_agent.domain.file_restore import extract_file_refs
from uwazi_admin_agent.domain.publish_state import PublishState
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
from uwazi_admin_agent.ports.publish_state_port import PublishStatePort
from uwazi_admin_agent.ports.relationship_hub_port import RelationshipHubPort


class VerifyRevertUseCase:
    """Verify a run's revert by fetching current raws and comparing to snapshots."""

    def __init__(
        self,
        entity_repository: EntityRepositoryPort,
        backup_store: BackupStorePort,
        file_repository: FileRepositoryPort | None = None,
        cache_control: CacheInvalidationPort | None = None,
        publish_state: PublishStatePort | None = None,
        relationship_hubs: RelationshipHubPort | None = None,
    ) -> None:
        self._entity_repository: EntityRepositoryPort = entity_repository
        self._backup_store: BackupStorePort = backup_store
        self._file_repository: FileRepositoryPort | None = file_repository
        self._cache_control: CacheInvalidationPort | None = cache_control
        self._publish_state: PublishStatePort | None = publish_state
        self._relationship_hubs: RelationshipHubPort | None = relationship_hubs

    async def verify(self, run_id: str) -> RevertVerificationResult:
        """Load manifest + snapshots, fetch current raws, return the verification result."""
        manifest = self._backup_store.load_manifest(run_id)
        snapshots = {
            sid: self._backup_store.load_snapshot(run_id, sid)
            for sid in {e.shared_id for e in (*manifest.modified, *manifest.deleted)}
        }
        fetch_keys = self._build_fetch_keys(manifest, snapshots)
        self._invalidate_checks(fetch_keys, manifest)  # invalidate-then-refetch
        try:
            current_raws = await self._fetch_current_raws(fetch_keys)
            deleted_file_gaps = await self._deleted_file_gaps(manifest, run_id)
            current_publish_states = await self._current_publish_states(manifest, snapshots)
            current_hub_rows = await self._current_hub_rows(manifest)
        finally:
            # Drop what our refetch just re-populated. Verification is a one-shot
            # audit, not a cache-warming read: the entities it checks are exactly the
            # ones a NEXT task's discovery would read, and a cached raw carries the
            # files JOIN (documents/attachments) at refetch time, so a later
            # file-delete could read a view the cache never invalidates. Leaving the
            # cache empty costs one fetch and keeps that window closed by
            # construction. No-op without a wired cache port.
            self._invalidate_checks(fetch_keys, manifest)
        result = verify_revert(
            manifest, snapshots, current_raws, deleted_file_gaps, current_publish_states, current_hub_rows
        )
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

    async def _current_publish_states(self, manifest: Any, snapshots: dict[str, Any]) -> dict[str, PublishState] | None:
        """Post-revert grants for every modified entity that captured some.

        Keyed by sharedId. Unwired port or an unreadable entity (e.g. it was
        deleted since) leaves that entity out — an absent entry is not checked,
        so this can add evidence but never a false mismatch.
        """
        if self._publish_state is None:
            return None
        states: dict[str, PublishState] = {}
        for entry in manifest.modified:
            snapshot = snapshots.get(entry.shared_id)
            if getattr(snapshot, "publish_state", None) is None:
                continue
            try:
                states[entry.shared_id] = await self._publish_state.read(entry.shared_id, entry.language)
            except Exception as exc:  # noqa: BLE001 — best-effort read
                logger.warning("verify: publish state unreadable sharedId={}: {}", entry.shared_id, exc)
        return states

    async def _current_hub_rows(self, manifest: Any) -> dict[str, list[dict[str, Any]]] | None:
        """Post-revert connection rows for every modified entity that captured some."""
        if self._relationship_hubs is None:
            return None
        rows: dict[str, list[dict[str, Any]]] = {}
        for entry in manifest.modified:
            try:
                snapshot = self._backup_store.load_snapshot(manifest.run_id, entry.shared_id)
            except FileNotFoundError:
                continue
            if not snapshot.connection_hubs:
                continue
            try:
                rows[entry.shared_id] = await self._relationship_hubs.list_for_entity(entry.shared_id, entry.language)
            except Exception as exc:  # noqa: BLE001 — best-effort read
                logger.warning("verify: connection rows unreadable sharedId={}: {}", entry.shared_id, exc)
        return rows

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
        actual = await self._actual_signatures(shared_id, self._records_language(records))
        return build_deleted_file_gaps(shared_id, expected, actual)

    @staticmethod
    def _records_language(records: list[Any]) -> str | None:
        """The entity row language its deleted-file records were captured on.

        Every record for one entity was read from (and will be re-uploaded to) the
        same row, so the first known language is the row to verify against.
        """
        for record in records:
            language = getattr(record, "language", None)
            if isinstance(language, str) and language:
                return language
        return None

    async def _expected_signature(self, record: Any, run_id: str) -> FileContentSignature:
        """One record's content signature from its captured bytes (wildcard on load failure)."""
        try:
            data = self._backup_store.load_file_bytes(run_id, record.shared_id, record.file_id)
        except Exception:  # noqa: BLE001 — best-effort: unknown bytes degrade to a wildcard digest
            return (record.kind, record.originalname, None)
        return (record.kind, record.originalname, file_digest(data))

    async def _actual_signatures(self, shared_id: str, language: str | None = None) -> list[FileContentSignature]:
        """The entity's CURRENT uploaded files' content signatures (wildcard on fetch failure)."""
        assert self._file_repository is not None  # guarded by _deleted_file_gaps
        raw = await self._fetch_optional(shared_id, language)
        if raw is None:
            return []  # the entity vanished post-revert: every expected file is a gap
        signatures: list[FileContentSignature] = []
        for ref in extract_file_refs(raw):
            data = await self._file_repository.get_file_bytes(ref.filename)
            digest = file_digest(data) if data is not None else None
            signatures.append((ref.kind, ref.originalname, digest))
        return signatures

    def _invalidate_checks(self, fetch_keys: list[tuple[str, str, str | None]], manifest: Any) -> None:
        """Drop every cached raw the checks are about to fetch (no-op without a cache port).

        Belt-and-suspenders freshness: our writes invalidate as they happen,
        but verification must never READ THROUGH an entry our own writes
        could have made stale. Includes the still-existing entities carrying
        deleted-file records (the content checks fetch those raws too, and
        they are not part of the manifest's entity lists).
        """
        if self._cache_control is None:
            return
        ids = {target for _key, target, _language in fetch_keys}
        ids.update(_distinct_file_entity_ids(manifest))
        if ids:
            self._cache_control.invalidate_entities(sorted(ids))

    async def _fetch_current_raws(self, fetch_keys: list[tuple[str, str, str | None]]) -> dict[str, dict[str, Any] | None]:
        """Fetch the current raw for every fetch key (``None`` if absent)."""
        current_raws: dict[str, dict[str, Any] | None] = {}
        for key, target, language in fetch_keys:
            current_raws[key] = await self._fetch_optional(target, language)
        return current_raws

    def _fetch_keys_for(self, result_key: str, target: str, snapshots: dict[str, Any]) -> list[tuple[str, str, str | None]]:
        """One fetch key per language row of an entity's snapshot.

        The primary row's key is the bare sharedId (legacy comparisons read
        ``current_raws[shared_id]``); each additional captured row gets
        ``sharedId|lang``. A legacy/single-row snapshot emits exactly one key —
        the previous behavior. Deletes fetch by ``restored_shared_id`` (the
        re-created entity exists only under the NEW id).
        """
        snapshot = snapshots.get(result_key)
        rows = getattr(snapshot, "rows", None) if snapshot is not None else []
        if rows:
            keys: list[tuple[str, str, str | None]] = []
            for index, row in enumerate(rows):
                language = row.language or None
                key = result_key if index == 0 else f"{result_key}|{language}"
                keys.append((key, target, language))
            return keys
        return [(result_key, target, self._language_of_snapshot(snapshot, result_key))]

    @staticmethod
    def _language_of_snapshot(snapshot: Any, shared_id: str) -> str | None:
        """The fallback row language when the snapshot has no ``rows`` list."""
        language = getattr(snapshot, "language", None) if snapshot is not None else None
        return language if isinstance(language, str) and language else None

    def _build_fetch_keys(self, manifest: Any, snapshots: dict[str, Any]) -> list[tuple[str, str, str | None]]:
        """The ``(result key, fetch target, row language)`` triples verification will read.

        A sharedId can have **one row per configured language** (Uwazi multilingual
        entities), and each row has its own ``_id``, its own ``language`` and its own
        i18n-TRANSLATED metadata ``label`` values. A snapshot is therefore only
        comparable against the SAME row — fetching without a language lets the server
        return whichever row it likes first (Uwazi's default locale), and comparing the
        Spanish snapshot against the English row flags a false mismatch on ``_id``,
        ``language`` and every translated label. So each fetch carries the language the
        state was recorded/reverted in: the manifest entry's ``language`` (populated
        from the snapshot's row at backup time) for modified/deleted/created, and the
        hub row's ``entityData.language`` for the inbound-ref endpoints.

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
        fetch_keys: list[tuple[str, str, str | None]] = []
        for entry in manifest.modified:
            # One fetch key PER snapshot row (the primary row keeps the bare
            # sharedId; secondary languages are keyed ``sharedId|lang``) so a
            # multi-language snapshot verifies every row the revert restored.
            fetch_keys.extend(self._fetch_keys_for(entry.shared_id, entry.shared_id, snapshots))
        for entry in manifest.deleted:
            target = entry.restored_shared_id or entry.shared_id
            fetch_keys.extend(self._fetch_keys_for(entry.shared_id, target, snapshots))
        for entry in manifest.created:
            fetch_keys.append((entry.shared_id, entry.shared_id, entry.language))

        deleted_ids = {e.shared_id for e in manifest.deleted}
        deleted_snapshots = {sid: snapshots[sid] for sid in deleted_ids if sid in snapshots}
        manifest_ids = (
            {e.shared_id for e in manifest.modified}
            | {e.shared_id for e in manifest.deleted}
            | {e.shared_id for e in manifest.created}
        )
        inbound_refs = extract_inbound_refs_from_existing(deleted_snapshots, deleted_ids, excluded_existing=manifest_ids)
        for ref in inbound_refs:
            fetch_keys.append((ref.existing_shared_id, ref.existing_shared_id, ref.existing_language))

        return fetch_keys

    @staticmethod
    def _language_of(entry: Any, snapshots: dict[str, Any]) -> str | None:
        """The row language a manifest entry's before-state was captured on.

        Prefers the entry's own ``language`` (recorded from the snapshot at backup
        time), falling back to the loaded snapshot's — older manifests may predate
        the field. None keeps the fetch language-agnostic (last resort).
        """
        language = getattr(entry, "language", None)
        if isinstance(language, str) and language:
            return language
        snapshot = snapshots.get(entry.shared_id)
        snapshot_language = getattr(snapshot, "language", None)
        return snapshot_language if isinstance(snapshot_language, str) and snapshot_language else None

    async def _fetch_optional(self, shared_id: str, language: str | None = None) -> dict[str, Any] | None:
        """Fetch a raw by sharedId + row language; ``None`` if the entity is absent (deleted).

        The language is passed through as the request's ``locale`` so a
        multilingual entity resolves to the SAME row the snapshot was captured on.
        Uwazi falls back to its default locale when that row does not exist; a
        returned row in a different language is logged, because the comparison it
        feeds can then never match (and the operator deserves to know the row the
        revert targeted is gone rather than guessing from a multi-KB diff).
        """
        try:
            raw = await self._entity_repository.get_raw_by_shared_id(shared_id, language)
        except Exception:
            # A not-found entity is expected for created (revert deleted them) and
            # for a deleted-entity whose re-create failed (a mismatch the verifier
            # reports). Any other fetch error is also surfaced as "absent" so the
            # verifier flags it rather than crashing the run.
            return None
        row_language = raw.get("language") if isinstance(raw, dict) else None
        if language and isinstance(row_language, str) and row_language and row_language != language:
            logger.warning(
                "verify: sharedId={} has no {} row (server returned the {} row)",
                shared_id,
                language,
                row_language,
            )
        return raw


def _distinct_file_entity_ids(manifest: Any) -> list[str]:
    """The distinct entities carrying deleted-file records, first-appearance order."""
    seen: set[str] = set()
    ordered: list[str] = []
    for record in manifest.deleted_files:
        if record.shared_id not in seen:
            seen.add(record.shared_id)
            ordered.append(record.shared_id)
    return ordered
