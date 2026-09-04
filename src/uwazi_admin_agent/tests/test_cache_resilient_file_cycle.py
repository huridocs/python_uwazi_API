"""THE regression: the delete → revert → re-run cycle stays cache-honest.

The operator's reported bug, reproduced end to end: "when I reverted the
operations back and try to run the script again, it's not working". Root
cause (verified against the Uwazi backend): file rows are not entity rows —
a raw's ``documents``/``attachments`` are a runtime JOIN from the files
collection by sharedId (``app/api/entities/entities.js`` ``withDocuments``)
— so a file DELETE mutates the files COLLECTION, and the existing write-path
invalidation (entity-ROW writes only) never learned about it. The cached raw
kept listing the deleted files; revert's re-uploads minted FRESH ids/filenames
(Uwazi never rewrites them) that the stale raw could not see; a re-run's
discovery then read ghosts and missed the restored duplicates.

What this file pins, against REAL adapters end to end:

- ``MiniUwazi`` is a real in-memory Uwazi miniature implementing BOTH ports
  (the ``UwaziApiAdapter`` multi-inheritance precedent): entity rows + file
  rows JOINed on read, uploads minting fresh ids + storage filenames, deletes
  dropping the row + bytes. The caches over it are the REAL
  ``CachedEntityRepository`` / ``CachedFileRepository`` over a REAL
  ``FileCacheStore`` rooted in ``tmp_path`` (the ``test_file_cache.py``
  precedent), and the deletion helpers run through the REAL
  ``BackupIntercept`` (``cache_control`` wired) via ``build_real_exec_namespace``.
- the acceptance cycle: dedupe-delete → the affected entity's cached raw is
  DROPPED and the deleted files' cached bytes EVICTED (the keeper's retained)
  → revert re-uploads → the re-upload target's cached raw is dropped again →
  a NEW task's re-run rediscovers the RESTORED duplicates (fresh ids) and
  deletes them with ZERO ghost ``failed`` noise → post-revert verification
  reads live truth. Cache eviction is lossless by construction: the cache is
  a read-through mirror (truth = Uwazi + the run's backup store), and entries
  repopulate lazily on the next read — the operator's "re-cache" ask.
- a partial batch that dies on a hard error STILL invalidates (recording and
  invalidation run on the script thread BEFORE the error is re-raised, so a
  partial batch stays revertable AND fresh).
- verification is invalidate-then-refetch: a FAILED re-upload must surface as
  a gap even though the cache still holds the pre-delete raw + bytes that
  would (wrongly) confirm the restore.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, override

import pytest

from uwazi_admin_agent.adapters.cached_entity_repository import CachedEntityRepository
from uwazi_admin_agent.adapters.cached_file_repository import CachedFileRepository
from uwazi_admin_agent.adapters.file_cache_store import FileCacheStore
from uwazi_admin_agent.domain.manifest import MigrationManifest, RunStatus
from uwazi_admin_agent.ports.backup_store_port import BackupStorePort
from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort
from uwazi_admin_agent.ports.file_repository_port import FileRepositoryPort
from uwazi_admin_agent.use_cases.backup_intercept import BackupIntercept
from uwazi_admin_agent.use_cases.revert_run_use_case import RevertRunUseCase
from uwazi_admin_agent.use_cases.script_exec_namespace import build_real_exec_namespace
from uwazi_admin_agent.use_cases.verify_revert_use_case import VerifyRevertUseCase

pytestmark = pytest.mark.anyio


class MiniUwazi(EntityRepositoryPort, FileRepositoryPort):
    """A real in-memory Uwazi miniature: entity ROWS + file ROWS, JOINed on read.

    Mirrors the production facts the cycle depends on:

    - ``documents``/``attachments`` on a fetched raw are a runtime JOIN from
      the files "collection" by sharedId — nothing about file rows is stored
      on the entity row, and ``save_raw`` never writes file rows (the JOIN
      artifacts are stripped);
    - every upload mints a FRESH file ``_id`` + storage ``filename``
      (``app/api/files/filesystem.ts``) and never rewrites them;
    - deleting a file removes its row + its bytes (a later fetch by the old
      storage filename is ``None``).

    Public dicts (``entity_rows``/``file_rows``/``bytes_store``) are the
    "collections", seeded directly by the tests — plain real state, no mocks.
    """

    def __init__(self, *, fail_uploads: bool = False) -> None:
        self.entity_rows: dict[str, dict[str, Any]] = {}
        self.file_rows: dict[str, dict[str, Any]] = {}
        self.bytes_store: dict[str, bytes] = {}
        self.fail_uploads: bool = fail_uploads
        self.uploads: list[tuple[str, str, str, str]] = []  # (kind, sharedId, originalname, content_type)
        self.deleted_file_ids: list[str] = []
        self.raw_gets: list[tuple[str, str | None]] = []  # INNER reads (cache-miss evidence)
        self._next_id: int = 0

    # --- entity rows (with the runtime JOIN) ---------------------------------

    @override
    async def get_raw_by_shared_id(self, shared_id: str, language: str | None = None) -> dict[str, Any]:
        self.raw_gets.append((shared_id, language))
        if shared_id not in self.entity_rows:
            raise RuntimeError(f"entity not found: {shared_id}")
        raw = dict(self.entity_rows[shared_id])
        files = [row for row in self.file_rows.values() if row["sharedId"] == shared_id]
        raw["documents"] = [dict(row) for row in files if row["type"] == "document"]
        raw["attachments"] = [dict(row) for row in files if row["type"] == "attachment"]
        return raw

    @override
    async def get_raw_by_internal_id(self, internal_id: str) -> dict[str, Any]:
        raise NotImplementedError("the cycle never reads by internal id")

    @override
    async def save_raw(self, raw: dict[str, Any]) -> None:
        # Strip the JOIN artifacts: an entity-row save never writes file rows.
        self.entity_rows[raw["sharedId"]] = {k: v for k, v in raw.items() if k not in ("documents", "attachments")}

    @override
    async def create_raw(self, raw: dict[str, Any]) -> str:
        self._next_id += 1
        new_sid = f"new-{self._next_id}"
        row = {k: v for k, v in raw.items() if k not in ("_id", "sharedId", "documents", "attachments")}
        row["sharedId"] = new_sid
        self.entity_rows[new_sid] = row
        return new_sid

    @override
    async def delete_by_shared_id(self, shared_id: str) -> None:
        self.entity_rows.pop(shared_id, None)
        for file_id, row in list(self.file_rows.items()):
            if row["sharedId"] == shared_id:
                del self.file_rows[file_id]
                self.bytes_store.pop(row["filename"], None)

    # --- file rows + bytes -----------------------------------------------------

    @override
    async def get_file_bytes(self, filename: str) -> bytes | None:
        return self.bytes_store.get(filename)

    @override
    async def upload_document(
        self, data: bytes, shared_id: str, language: str | None, title: str, content_type: str
    ) -> bool:
        return self._append_file("document", data, shared_id, title, content_type)

    @override
    async def upload_attachment(
        self, data: bytes, shared_id: str, language: str | None, title: str, content_type: str
    ) -> bool:
        return self._append_file("attachment", data, shared_id, title, content_type)

    @override
    async def delete_file(self, file_id: str) -> bool:
        row = self.file_rows.pop(file_id, None)
        if row is None:
            return False
        self.bytes_store.pop(row["filename"], None)
        self.deleted_file_ids.append(file_id)
        return True

    def _append_file(
        self, type_: Literal["document", "attachment"], data: bytes, shared_id: str, title: str, content_type: str
    ) -> bool:
        """Mint a FRESH id + storage filename and append one file row (Uwazi never reuses)."""
        if self.fail_uploads:
            return False
        self._next_id += 1
        file_id = f"n{self._next_id}"
        filename = f"storage-{self._next_id}"
        self.file_rows[file_id] = {
            "_id": file_id,
            "sharedId": shared_id,
            "type": type_,
            "originalname": title,
            "filename": filename,
            "size": len(data),
        }
        self.bytes_store[filename] = data
        self.uploads.append((type_, shared_id, title, content_type))
        return True


class InMemoryBackupStore(BackupStorePort):
    """Manifests, snapshots, and captured file bytes in plain dicts.

    The ``test_file_delete_revert.py`` shape, re-declared so this file stays
    self-contained.
    """

    def __init__(self) -> None:
        self._manifests: dict[str, MigrationManifest] = {}
        self._snapshots: dict[str, dict[str, Any]] = {}
        self._file_bytes: dict[tuple[str, str, str], bytes] = {}

    @override
    def save_snapshot(self, run_id: str, snapshot: Any) -> None:
        self._snapshots.setdefault(run_id, {})[snapshot.shared_id] = snapshot

    @override
    def load_snapshot(self, run_id: str, shared_id: str) -> Any:
        if shared_id not in self._snapshots.get(run_id, {}):
            raise FileNotFoundError(f"No snapshot for run={run_id} sharedId={shared_id}")
        return self._snapshots[run_id][shared_id]

    @override
    def save_file_bytes(self, run_id: str, shared_id: str, file_id: str, data: bytes) -> None:
        self._file_bytes[(run_id, shared_id, file_id)] = data

    @override
    def load_file_bytes(self, run_id: str, shared_id: str, file_id: str) -> bytes:
        key = (run_id, shared_id, file_id)
        if key not in self._file_bytes:
            raise FileNotFoundError(f"No file bytes for run={run_id} sharedId={shared_id} fileId={file_id}")
        return self._file_bytes[key]

    @override
    def save_manifest(self, run_id: str, manifest: MigrationManifest) -> None:
        self._manifests[run_id] = manifest

    @override
    def load_manifest(self, run_id: str) -> MigrationManifest:
        return self._manifests[run_id]

    @override
    def update_status(self, run_id: str, status: RunStatus) -> None:
        self._manifests[run_id].status = status

    @override
    def clear_run(self, run_id: str) -> None:
        for key in list(self._file_bytes):
            if key[0] == run_id:
                self._file_bytes.pop(key)

    @override
    def list_runs(self) -> list[str]:
        return sorted(self._manifests.keys())

    @override
    def delete_run(self, run_id: str) -> None:
        self._manifests.pop(run_id, None)

    @override
    def rename_run(self, old_id: str, new_id: str) -> None: ...


# --- the incident seed (the operator's real shape) ------------------------------


def _seed_incident(uwazi: MiniUwazi) -> None:
    """E1: three byte-identical Spanish copies + one genuine English original
    of the same name, two byte-identical HTML attachments, and a connection
    citing one of the redundant copies (the incident entity's shape)."""
    uwazi.entity_rows["E1"] = {
        "_id": "o-E1",
        "sharedId": "E1",
        "title": "Incident",
        "language": "en",
        "relations": [{"entity": "E1", "file": "d3"}],  # a text reference cites the d3 copy
    }
    docs = [
        ("d1", "a.pdf", "f1", b"SPANISH"),
        ("d2", "a.pdf", "f2", b"SPANISH"),
        ("d3", "a.pdf", "f3", b"SPANISH"),
        ("d4", "a.pdf", "f4", b"ENGLISH"),
    ]
    attachments = [("h1", "doc.html", "g1", b"HTML"), ("h2", "doc.html", "g2", b"HTML")]
    for file_id, name, filename, data in docs:
        uwazi.file_rows[file_id] = {
            "_id": file_id,
            "sharedId": "E1",
            "type": "document",
            "originalname": name,
            "filename": filename,
            "size": len(data),
        }
        uwazi.bytes_store[filename] = data
    for file_id, name, filename, data in attachments:
        uwazi.file_rows[file_id] = {
            "_id": file_id,
            "sharedId": "E1",
            "type": "attachment",
            "originalname": name,
            "filename": filename,
            "size": len(data),
        }
        uwazi.bytes_store[filename] = data


# --- wiring helpers (real adapters + real caches throughout) ---------------------


def _wired(
    tmp_path: Path, *, fail_uploads: bool = False
) -> tuple[MiniUwazi, FileCacheStore, CachedEntityRepository, CachedFileRepository, InMemoryBackupStore]:
    """The fully wired cycle: a real instance + real cached repos + a real store."""
    uwazi = MiniUwazi(fail_uploads=fail_uploads)
    _seed_incident(uwazi)
    cache = FileCacheStore(root=tmp_path / "cache", max_bytes=10**9, ttl_seconds=600.0, evict_scan_interval=10**9)
    entity_repository = CachedEntityRepository(uwazi, cache)
    file_repository = CachedFileRepository(uwazi, cache)
    return uwazi, cache, entity_repository, file_repository, InMemoryBackupStore()


def _task_namespace(
    cache: FileCacheStore,
    entity_repository: CachedEntityRepository,
    file_repository: CachedFileRepository,
    backup_store: InMemoryBackupStore,
    run_id: str,
) -> tuple[dict[str, Any], MigrationManifest]:
    """A NEW task's exec namespace: a fresh manifest + intercept over the SAME
    caches (the persistent cross-task cache is exactly what the bug lived in)."""
    manifest = MigrationManifest(
        run_id=run_id,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        prompt="dedupe the incident entity",
        script="dedupe_entity_files_parallel(['E1'])",
        status=RunStatus.EXECUTED,
    )
    backup_store.save_manifest(run_id, manifest)
    intercept = BackupIntercept(
        entity_repository=entity_repository,
        backup_store=backup_store,
        manifest=manifest,
        run_id=run_id,
        language="en",
        loop=asyncio.new_event_loop(),
        audit_log=None,
        cap=1000,
        file_repository=file_repository,
        cache_control=cache,
    )
    ns = build_real_exec_namespace(
        entity_api=None,  # type: ignore[arg-type]
        relationship_api=None,
        loop=asyncio.new_event_loop(),
        intercept=intercept,
        tool_cache=None,
        default_language="en",
        entity_repository=entity_repository,
        file_repository=file_repository,
    )
    return ns, manifest


# --- THE acceptance cycle --------------------------------------------------------


async def test_delete_revert_rerun_cycle_through_real_caches(tmp_path: Path) -> None:
    """The operator's exact scenario: dedupe-delete → revert → a NEW task's
    re-run must rediscover the restored duplicates (fresh ids) and delete them
    again with zero ghost ``failed`` noise — the caches stay honest at every
    mutation of the files collection."""
    uwazi, cache, entity_repository, file_repository, backup_store = _wired(tmp_path)
    ns, manifest = _task_namespace(cache, entity_repository, file_repository, backup_store, "run-1")

    # Task 1 — the dedupe delete. Its own discovery runs through the cached
    # repos, so the caches are warm when the deletes land.
    [summary] = ns["dedupe_entity_files_parallel"](["E1"])
    assert summary == {"shared_id": "E1", "duplicates": 3, "deleted": 2, "failed": 0, "kept_cited": 1}
    assert uwazi.deleted_file_ids == ["d2", "h2"]
    assert [r.file_id for r in manifest.deleted_files] == ["d2", "h2"]
    # The delete dropped the affected entity's cached raw...
    assert cache.get_raw("E1", "en") is None
    # ...and evicted the DELETED files' cached bytes (successful deletes only).
    assert cache.get_file_bytes("f2") is None  # d2's storage filename
    assert cache.get_file_bytes("g2") is None  # h2's storage filename
    # The keepers' bytes survive (the keeper d1, the CITED keeper d3, the
    # unique English original d4, the attachment keeper h1).
    assert cache.get_file_bytes("f1") == b"SPANISH"
    assert cache.get_file_bytes("f3") == b"SPANISH"
    assert cache.get_file_bytes("f4") == b"ENGLISH"
    assert cache.get_file_bytes("g1") == b"HTML"
    # Exactly the run's own invalidations: 1 entity raw dir + 2 byte entries.
    assert cache.snapshot_stats().invalidations == 3

    # A read through the cached repo now RE-CACHES live post-delete truth (the
    # operator's "re-cache": entries repopulate lazily, never data).
    raw = await entity_repository.get_raw_by_shared_id("E1", "en")
    assert [d["_id"] for d in raw["documents"]] == ["d1", "d3", "d4"]
    assert cache.get_raw("E1", "en") is not None

    # Revert — the re-uploaded duplicates get FRESH ids + storage filenames.
    await RevertRunUseCase(
        entity_repository=entity_repository,
        backup_store=backup_store,
        file_repository=file_repository,
        cache_control=cache,
    ).revert("run-1")
    assert [u[:3] for u in uwazi.uploads] == [("document", "E1", "a.pdf"), ("attachment", "E1", "doc.html")]
    # THE revert-side assertion: the re-upload target's cached raw (repopulated
    # above) is dropped again — a post-revert read must see the restored files.
    assert cache.get_raw("E1", "en") is None
    raw = await entity_repository.get_raw_by_shared_id("E1", "en")
    assert [d["_id"] for d in raw["documents"]] == ["d1", "d3", "d4", "n1"]
    assert [a["_id"] for a in raw["attachments"]] == ["h1", "n2"]
    assert raw["documents"][3]["filename"] == "storage-1"  # fresh storage name

    # Post-revert verification reads live truth and passes by CONTENT.
    verification = await VerifyRevertUseCase(
        entity_repository=entity_repository,
        backup_store=backup_store,
        file_repository=file_repository,
        cache_control=cache,
    ).verify("run-1")
    assert verification.ok
    assert verification.file_gaps == []

    # Task 2 — a NEW task with the SAME persistent caches re-runs the discovery.
    ns2, manifest2 = _task_namespace(cache, entity_repository, file_repository, backup_store, "run-2")
    raw_gets_before = len(uwazi.raw_gets)
    [rerun] = ns2["dedupe_entity_files_parallel"](["E1"])
    # The re-run saw the RESTORED duplicates (fresh ids n1/n2 — the stale raw
    # would have shown ghosts d2/h2 instead, deleting nothing, failing twice).
    assert rerun["deleted"] == 2
    assert rerun["failed"] == 0
    assert rerun["kept_cited"] == 1
    assert uwazi.deleted_file_ids == ["d2", "h2", "n1", "n2"]
    assert [r.file_id for r in manifest2.deleted_files] == ["n1", "n2"]
    # The discovery actually re-fetched the raw from the instance (the revert
    # + delete invalidations left the cache empty for it).
    assert len(uwazi.raw_gets) == raw_gets_before + 1
    # And the second run's deletes evicted the restored copies' fresh bytes.
    assert cache.get_file_bytes("storage-1") is None
    assert cache.get_file_bytes("storage-2") is None
    assert cache.get_file_bytes("f1") == b"SPANISH"  # the keeper is still cached


# --- partial batches invalidate before the hard error -----------------------------


async def test_partial_batch_invalidates_before_the_hard_error(tmp_path: Path) -> None:
    """A batch where one entity's fetch RAISES: the other entity's applied
    deletes are recorded AND its caches dropped on the script thread BEFORE the
    error is re-raised — a partial batch stays revertable AND fresh."""
    uwazi, cache, entity_repository, file_repository, backup_store = _wired(tmp_path)
    # Warm the caches with a plain discovery read (the raw + one duplicate's bytes).
    _ = await entity_repository.get_raw_by_shared_id("E1", "en")
    _ = await file_repository.get_file_bytes("f2")
    assert cache.get_raw("E1", "en") is not None

    ns, manifest = _task_namespace(cache, entity_repository, file_repository, backup_store, "run-1")
    # E2 does not exist: its task dies on a hard fetch error after E1's deletes.
    with pytest.raises(RuntimeError, match="entity not found: E2"):
        ns["dedupe_entity_files_parallel"](["E1", "E2"])

    # The partial batch was recorded (revertable)...
    assert [r.file_id for r in manifest.deleted_files] == ["d2", "h2"]
    # ...AND cache-fresh even though the batch died: E1's raw + the deleted
    # files' bytes are gone, the keeper's bytes stay.
    assert cache.get_raw("E1", "en") is None
    assert cache.get_file_bytes("f2") is None
    assert cache.get_file_bytes("g2") is None
    assert cache.get_file_bytes("f1") == b"SPANISH"


# --- verification reads live truth, not the stale cache ---------------------------


async def test_verify_reads_live_truth_despite_stale_cache(tmp_path: Path) -> None:
    """A FAILED re-upload must surface as a file gap even though the cache
    still holds the pre-delete raw + bytes that would confirm the restore
    (invalidate-then-refetch in verification; the keeper files cannot mask
    this gap because the deleted file was the entity's ONLY document)."""
    uwazi, cache, entity_repository, file_repository, backup_store = _wired(tmp_path, fail_uploads=True)
    # Nothing is cited in this scenario (the incident's citation is removed up
    # front): the operator names ALL six files, and all six must be deletable.
    uwazi.entity_rows["E1"]["relations"] = []
    ns, _manifest = _task_namespace(cache, entity_repository, file_repository, backup_store, "run-1")
    # Explicitly delete ALL of E1's files (the operator naming them precisely).
    [summary] = ns["delete_entity_files_parallel"](
        [
            {"shared_id": "E1", "file_id": "d1"},
            {"shared_id": "E1", "file_id": "d2"},
            {"shared_id": "E1", "file_id": "d3"},
            {"shared_id": "E1", "file_id": "d4"},
            {"shared_id": "E1", "file_id": "h1"},
            {"shared_id": "E1", "file_id": "h2"},
        ]
    )
    assert summary["deleted"] == 6
    assert cache.get_raw("E1", "en") is None  # the delete invalidated the raw
    assert cache.get_file_bytes("f1") is None  # and evicted the deleted bytes

    # Re-plant the stale view the hard way: read E1 through the cached repo and
    # confirm the post-delete truth caches; the revert then FAILS every upload.
    raw = await entity_repository.get_raw_by_shared_id("E1", "en")
    assert raw["documents"] == [] and raw["attachments"] == []
    await RevertRunUseCase(
        entity_repository=entity_repository,
        backup_store=backup_store,
        file_repository=file_repository,
        cache_control=cache,
    ).revert("run-1")
    assert uwazi.uploads == []  # the instance refused every re-upload

    verification = await VerifyRevertUseCase(
        entity_repository=entity_repository,
        backup_store=backup_store,
        file_repository=file_repository,
        cache_control=cache,
    ).verify("run-1")
    # Live truth: nothing came back — six gaps. Reading the cached raw instead
    # would (wrongly) confirm the restore and pass.
    assert not verification.ok
    assert len(verification.file_gaps) == 6
    assert all(g.gap == "missing" for g in verification.file_gaps)
