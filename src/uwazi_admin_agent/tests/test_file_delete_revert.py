"""Isolated unit tests for deleted-file REVERT + post-revert verification.

Per AGENTS.md: no mocks/stubs, no network, no running Uwazi instance. The
revert + verify use cases are driven against tiny REAL in-memory port
classes (the ``test_revert_run_use_case.py`` precedent — the same
InMemoryEntityRepository/InMemoryBackupStore/InMemoryFileRepository shapes,
re-declared here so each test file stays self-contained). What is pinned:

- a pure-file-delete run re-uploads each deleted file's captured bytes to the
  STILL-EXISTING entity's sharedId (fresh identity, documents first);
- a file deleted from an entity the SAME run also deleted re-uploads to the
  entity's re-created NEW sharedId (recreate-then-restore ordering);
- verification is by CONTENT — the multiset of (kind, originalname, sha256)
  — because Uwazi mints fresh file ids/filenames on every upload;
- reverting a dedupe cleanup RE-CREATES the duplicates (the correct undo) and
  verification does NOT flag the extra copies as gaps;
- a re-created entity's file check merges the run's deleted-file records so
  restored deletes do not surface as false ``extra`` gaps;
- the revert gate refuses an already-REVERTED pure-file run (re-reverting
  would re-upload duplicates);
- the cap counts DISTINCT entities touched (entity writes + file deletes).
"""

from datetime import datetime, timezone
from typing import Any, override

import pytest

from uwazi_admin_agent.domain.cap_enforcement import touch_set_count
from uwazi_admin_agent.domain.deleted_file import DeletedFile
from uwazi_admin_agent.domain.manifest import EntityIdentity, MigrationManifest, RunStatus
from uwazi_admin_agent.domain.revert_gate import RevertRefusedError
from uwazi_admin_agent.domain.snapshot import EntitySnapshot
from uwazi_admin_agent.ports.backup_store_port import BackupStorePort
from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort
from uwazi_admin_agent.ports.file_repository_port import FileRepositoryPort
from uwazi_admin_agent.use_cases.revert_run_use_case import RevertRunUseCase
from uwazi_admin_agent.use_cases.verify_revert_use_case import VerifyRevertUseCase

pytestmark = pytest.mark.anyio


# --- in-memory ports (real classes, not mocks) ------------------------------


class InMemoryEntityRepository(EntityRepositoryPort):
    """Raw store keyed by sharedId; create mints a fresh sharedId like Uwazi."""

    def __init__(self, entities: dict[str, dict[str, Any]] | None = None) -> None:
        self._entities: dict[str, dict[str, Any]] = dict(entities or {})
        self._next_id = 0
        self.save_calls: list[dict[str, Any]] = []

    @override
    async def get_raw_by_shared_id(self, shared_id: str, language: str | None = None) -> dict[str, Any]:
        if shared_id not in self._entities:
            raise RuntimeError(f"Entity not found: {shared_id}")
        return dict(self._entities[shared_id])

    @override
    async def get_raw_by_internal_id(self, internal_id: str) -> dict[str, Any]:
        raise NotImplementedError

    @override
    async def save_raw(self, raw: dict[str, Any]) -> None:
        sid = raw.get("sharedId")
        if sid is None:
            raise RuntimeError("raw missing sharedId")
        self._entities[sid] = dict(raw)
        self.save_calls.append(dict(raw))

    @override
    async def create_raw(self, raw: dict[str, Any]) -> str:
        self._next_id += 1
        new_sid = f"new-{self._next_id}"
        stored = {k: v for k, v in raw.items() if k not in {"_id", "sharedId"}}
        stored["sharedId"] = new_sid
        self._entities[new_sid] = stored
        return new_sid

    @override
    async def delete_by_shared_id(self, shared_id: str) -> None:
        self._entities.pop(shared_id, None)

    def get(self, shared_id: str) -> dict[str, Any]:
        return self._entities[shared_id]

    def has(self, shared_id: str) -> bool:
        return shared_id in self._entities


class InMemoryBackupStore(BackupStorePort):
    """Manifests, snapshots, and captured file bytes in plain dicts."""

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


class InMemoryFileRepository(FileRepositoryPort):
    """Serves current file bytes; records uploads + fresh minted ids.

    Uploads append a FRESH file row to the target entity's raw (documents
    before attachments is the use case's ordering, not the repo's), mirroring
    Uwazi's mint-a-new-id-on-every-upload behavior.
    """

    def __init__(self, current_bytes: dict[str, bytes]) -> None:
        self._bytes = current_bytes
        self.uploads: list[tuple[str, str, str, str]] = []  # (kind, shared_id, originalname, content_type)
        self._next_id = 0

    @override
    async def get_file_bytes(self, filename: str) -> bytes | None:
        return self._bytes.get(filename)

    @override
    async def upload_document(
        self, data: bytes, shared_id: str, language: str | None, title: str, content_type: str
    ) -> bool:
        self.uploads.append(("document", shared_id, title, content_type))
        return True

    @override
    async def upload_attachment(
        self, data: bytes, shared_id: str, language: str | None, title: str, content_type: str
    ) -> bool:
        self.uploads.append(("attachment", shared_id, title, content_type))
        return True

    @override
    async def delete_file(self, file_id: str) -> bool:
        raise NotImplementedError("revert never deletes file rows")


# --- helpers ----------------------------------------------------------------


def _now() -> datetime:
    return datetime(2024, 1, 1, tzinfo=timezone.utc)


def _record(shared_id: str, file_id: str, kind: str, name: str, source: str = "explicit") -> DeletedFile:
    return DeletedFile(
        shared_id=shared_id,
        file_id=file_id,
        kind=kind,  # type: ignore[arg-type]
        originalname=name,
        filename=f"storage-{file_id}",
        language="en",
        content_type="application/pdf" if kind == "document" else "text/html",
        size=7,
        source=source,  # type: ignore[arg-type]
    )


def _manifest(deleted_files: list[DeletedFile], deleted: list[EntityIdentity] | None = None) -> MigrationManifest:
    return MigrationManifest(
        run_id="run-1",
        created_at=_now(),
        prompt="d",
        script="x = 1",
        deleted=deleted or [],
        deleted_files=deleted_files,
        status=RunStatus.EXECUTED,
    )


def _snapshot(shared_id: str, raw: dict[str, Any]) -> EntitySnapshot:
    return EntitySnapshot(
        shared_id=shared_id,
        internal_id=raw.get("_id"),
        language=raw.get("language"),
        raw=raw,
        captured_at=_now(),
        files=[],
    )


def _raw(shared_id: str, *, documents: list | None = None, attachments: list | None = None) -> dict[str, Any]:
    return {
        "sharedId": shared_id,
        "_id": "o-" + shared_id,
        "title": "T",
        "language": "en",
        "documents": documents or [],
        "attachments": attachments or [],
    }


def _doc(file_id: str, name: str, filename: str) -> dict[str, Any]:
    return {"_id": file_id, "originalname": name, "filename": filename, "size": 7}


# --- revert: still-existing entity (pure file-delete run) ----------------------


async def test_revert_uploads_deleted_files_to_the_same_shared_id() -> None:
    """A pure-file-delete run (no entity snapshots at all): revert re-uploads
    each captured file to the STILL-EXISTING entity, documents first."""
    repo = InMemoryEntityRepository({"E1": _raw("E1")})
    store = InMemoryBackupStore()
    files = [_record("E1", "d2", "document", "a.pdf"), _record("E1", "h9", "attachment", "scan.html")]
    store.save_manifest("run-1", _manifest(files))
    store.save_file_bytes("run-1", "E1", "d2", b"PDF")
    store.save_file_bytes("run-1", "E1", "h9", b"HTML")

    file_repo = InMemoryFileRepository({})
    use_case = RevertRunUseCase(entity_repository=repo, backup_store=store, file_repository=file_repo)
    await use_case.revert("run-1")

    assert file_repo.uploads == [
        ("document", "E1", "a.pdf", "application/pdf"),
        ("attachment", "E1", "scan.html", "text/html"),
    ]
    assert store.load_manifest("run-1").status == RunStatus.REVERTED


async def test_revert_deleted_files_best_effort_missing_bytes() -> None:
    """A record whose bytes were never captured is skipped + audited, never
    fatal — the revert still completes (the gap surfaces in verification)."""
    repo = InMemoryEntityRepository({"E1": _raw("E1")})
    store = InMemoryBackupStore()
    store.save_manifest("run-1", _manifest([_record("E1", "d2", "document", "a.pdf")]))
    file_repo = InMemoryFileRepository({})
    use_case = RevertRunUseCase(entity_repository=repo, backup_store=store, file_repository=file_repo)
    await use_case.revert("run-1")
    assert file_repo.uploads == []
    assert store.load_manifest("run-1").status == RunStatus.REVERTED


# --- revert: entity also deleted in the same run ------------------------------


async def test_revert_uploads_deleted_files_to_the_recreated_shared_id() -> None:
    """Entity E was deleted by the run AND carried an explicit file delete
    from earlier in the same run: the file re-uploads to the entity's
    re-created NEW sharedId (recreate-then-restore ordering), never the dead
    old id."""
    repo = InMemoryEntityRepository(entities={})  # E1 was deleted
    store = InMemoryBackupStore()
    store.save_snapshot("run-1", _snapshot("E1", _raw("E1")))
    store.save_manifest(
        "run-1",
        _manifest(
            deleted_files=[_record("E1", "d9", "document", "b.pdf")],
            deleted=[EntityIdentity(shared_id="E1")],
        ),
    )
    store.save_file_bytes("run-1", "E1", "d9", b"PDF-B")
    file_repo = InMemoryFileRepository({})
    use_case = RevertRunUseCase(entity_repository=repo, backup_store=store, file_repository=file_repo)
    await use_case.revert("run-1")

    assert repo.has("new-1")  # E1 re-created under a fresh sharedId
    assert file_repo.uploads == [("document", "new-1", "b.pdf", "application/pdf")]


async def test_revert_of_a_dedupe_cleanup_recreates_the_duplicates() -> None:
    """The honest undo: reverting a dedupe cleanup re-uploads the removed
    duplicate copies — the entity ends up with the duplicates BACK (records
    carry source='dedupe'; the summary notes this in the driver)."""
    repo = InMemoryEntityRepository({"E1": _raw("E1")})
    store = InMemoryBackupStore()
    dupes = [_record("E1", f"d{i}", "document", "a.pdf", source="dedupe") for i in (2, 3)]
    store.save_manifest("run-1", _manifest(dupes))
    store.save_file_bytes("run-1", "E1", "d2", b"SPANISH")
    store.save_file_bytes("run-1", "E1", "d3", b"SPANISH")
    file_repo = InMemoryFileRepository({})
    use_case = RevertRunUseCase(entity_repository=repo, backup_store=store, file_repository=file_repo)
    await use_case.revert("run-1")

    assert file_repo.uploads == [
        ("document", "E1", "a.pdf", "application/pdf"),
        ("document", "E1", "a.pdf", "application/pdf"),
    ]
    assert store.load_manifest("run-1").status == RunStatus.REVERTED


# --- the revert gate for pure-file runs ---------------------------------------


async def test_revert_gate_refuses_an_already_reverted_file_run() -> None:
    """A pure-file-delete run reverts through the SAME status gate: a second
    revert is refused (it would re-upload copies that never went away)."""
    repo = InMemoryEntityRepository({"E1": _raw("E1")})
    store = InMemoryBackupStore()
    store.save_manifest("run-1", _manifest([_record("E1", "d2", "document", "a.pdf")]))
    store.save_file_bytes("run-1", "E1", "d2", b"PDF")
    file_repo = InMemoryFileRepository({})
    use_case = RevertRunUseCase(entity_repository=repo, backup_store=store, file_repository=file_repo)
    await use_case.revert("run-1")
    store.update_status("run-1", RunStatus.REVERTED)

    with pytest.raises(RevertRefusedError):
        await use_case.revert("run-1")


async def test_pure_file_run_reverts_with_no_entity_snapshots() -> None:
    """The gate + the action builder handle a manifest with ONLY
    ``deleted_files`` (no modified/deleted/created) — no snapshot loads."""
    repo = InMemoryEntityRepository({"E1": _raw("E1")})
    store = InMemoryBackupStore()
    store.save_manifest("run-1", _manifest([_record("E1", "d2", "document", "a.pdf")]))
    store.save_file_bytes("run-1", "E1", "d2", b"PDF")
    file_repo = InMemoryFileRepository({})
    use_case = RevertRunUseCase(entity_repository=repo, backup_store=store, file_repository=file_repo)
    await use_case.revert("run-1")
    assert file_repo.uploads == [("document", "E1", "a.pdf", "application/pdf")]


# --- verification: content multisets -------------------------------------------


async def test_verify_passes_when_restored_content_matches_by_digest() -> None:
    """Fresh ids/filenames make raw equality useless — the check is the
    multiset of (kind, originalname, sha256), and restored content passes
    even though every identity differs."""
    repo = InMemoryEntityRepository(
        {
            "E1": _raw(
                "E1",
                documents=[_doc("nd1", "a.pdf", "fresh-hash-1")],  # FRESH id + filename
                attachments=[{"_id": "nh1", "originalname": "scan.html", "filename": "fresh-hash-2", "size": 5}],
            )
        }
    )
    store = InMemoryBackupStore()
    store.save_manifest(
        "run-1",
        _manifest(
            [
                _record("E1", "d2", "document", "a.pdf"),
                _record("E1", "h9", "attachment", "scan.html"),
            ]
        ),
    )
    store.save_file_bytes("run-1", "E1", "d2", b"PDF")
    store.save_file_bytes("run-1", "E1", "h9", b"HTML")
    file_repo = InMemoryFileRepository({"fresh-hash-1": b"PDF", "fresh-hash-2": b"HTML"})

    result = await VerifyRevertUseCase(repo, store, file_repo).verify("run-1")
    assert result.ok
    assert result.file_gaps == []


async def test_verify_flags_missing_and_wrong_content_after_revert() -> None:
    """A file that did not come back, and a same-named file whose CONTENT
    differs, are both real gaps (the digest is the comparison key)."""
    repo = InMemoryEntityRepository(
        {"E1": _raw("E1", documents=[_doc("nd1", "a.pdf", "fresh")])}  # only one came back, wrong bytes
    )
    store = InMemoryBackupStore()
    store.save_manifest(
        "run-1",
        _manifest(
            [
                _record("E1", "d2", "document", "a.pdf"),
                _record("E1", "d3", "document", "a.pdf"),
            ]
        ),
    )
    store.save_file_bytes("run-1", "E1", "d2", b"EXPECTED")
    store.save_file_bytes("run-1", "E1", "d3", b"EXPECTED")
    file_repo = InMemoryFileRepository({"fresh": b"DIFFERENT"})

    result = await VerifyRevertUseCase(repo, store, file_repo).verify("run-1")
    assert not result.ok
    gaps = [(g.gap, g.kind, g.originalname) for g in result.file_gaps]
    assert gaps == [("missing", "document", "a.pdf"), ("missing", "document", "a.pdf")]


async def test_verify_allows_dedupe_duplicates_without_false_gaps() -> None:
    """A reverted dedupe cleanup leaves the KEEPER plus the restored
    duplicates — the containment check passes (extras are the correct undo,
    not gaps)."""
    repo = InMemoryEntityRepository(
        {
            "E1": _raw(
                "E1",
                documents=[
                    _doc("keep", "a.pdf", "keeper-hash"),
                    _doc("nd2", "a.pdf", "fresh-2"),
                    _doc("nd3", "a.pdf", "fresh-3"),
                ],
            )
        }
    )
    store = InMemoryBackupStore()
    dupes = [_record("E1", f"d{i}", "document", "a.pdf", source="dedupe") for i in (2, 3)]
    store.save_manifest("run-1", _manifest(dupes))
    store.save_file_bytes("run-1", "E1", "d2", b"SPANISH")
    store.save_file_bytes("run-1", "E1", "d3", b"SPANISH")
    file_repo = InMemoryFileRepository({"keeper-hash": b"SPANISH", "fresh-2": b"SPANISH", "fresh-3": b"SPANISH"})

    result = await VerifyRevertUseCase(repo, store, file_repo).verify("run-1")
    assert result.ok
    assert result.file_gaps == []


async def test_verify_without_file_repository_and_file_records_raises() -> None:
    repo = InMemoryEntityRepository({"E1": _raw("E1")})
    store = InMemoryBackupStore()
    store.save_manifest("run-1", _manifest([_record("E1", "d2", "document", "a.pdf")]))
    with pytest.raises(RuntimeError, match="wired file_repository"):
        await VerifyRevertUseCase(repo, store).verify("run-1")


# --- cap accounting: DISTINCT entities -----------------------------------------


def test_touch_set_counts_distinct_entities_not_files() -> None:
    manifest = _manifest(
        [
            _record("E1", "d1", "document", "a.pdf"),
            _record("E1", "d2", "document", "a.pdf"),
            _record("E1", "h1", "attachment", "s.html"),
            _record("E2", "x1", "document", "x.pdf"),
        ]
    )
    assert touch_set_count(manifest) == 2  # E1 + E2, not 4 files


def test_touch_set_does_not_double_count_entities_touched_twice() -> None:
    manifest = _manifest(
        [_record("E1", "d2", "document", "a.pdf")],
        deleted=[EntityIdentity(shared_id="E1")],
    )
    assert touch_set_count(manifest) == 1  # E1 was also entity-deleted — ONE entity
