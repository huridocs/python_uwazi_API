"""Use-case-level tests for the consecutive-execute refusal (the gate in action).

The operator's live report: an ``EXECUTED`` run could be executed AGAIN without
reverting first ("the script just was not able to properly dedup again"). The
hole: ``decide_execute_gate`` allowed every non-FAILED status, so the
re-execute path ran ``manifest.reset_touch_set()`` + ``backup_store.clear_run``
— wiping the manifest records AND the backed-up bytes that are the run's ONLY
path back — and then re-ran the script against the already-deleted state. The
run ended ``EXECUTED`` with an empty touch set while the first pass's deletes
stayed live in Uwazi: unrevertable.

What this file pins, through the REAL :class:`ExecuteScriptUseCase` (worker
thread, real intercept, real exec namespace) against a real in-memory Uwazi
miniature (the ``test_cache_resilient_file_cycle.py`` pattern, re-declared
self-contained and trimmed to the gate's needs — no caches here, they have
their own file):

- a CONSECUTIVE execute of an ``EXECUTED`` run is refused, and the refusal
  clears NOTHING: the touch set, the backed-up bytes, and the live instance
  state are exactly what pass 1 left — proven by then REVERTING the run and
  seeing both files come back;
- the ``REVERTED`` -> re-execute cycle the operator already relies on keeps
  working end to end: the re-execute is allowed, the reset + ``clear_run``
  fire, the re-run rediscovers the restored duplicates (FRESH ids — uploads
  never reuse them) and deletes them again, leaving a fresh, revertable
  touch set.

No mocks, no network: real in-memory port classes with literal data (the
AGENTS.md-sanctioned pattern).
"""

from datetime import datetime, timezone
from typing import Any, Literal, override

import pytest

from uwazi_admin_agent.domain.execute_gate import ExecuteRefusedError
from uwazi_admin_agent.domain.manifest import MigrationManifest, RunStatus
from uwazi_admin_agent.ports.backup_store_port import BackupStorePort
from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort
from uwazi_admin_agent.ports.file_repository_port import FileRepositoryPort
from uwazi_admin_agent.use_cases.execute_script_use_case import ExecuteScriptUseCase
from uwazi_admin_agent.use_cases.revert_run_use_case import RevertRunUseCase

pytestmark = pytest.mark.anyio

SCRIPT = "result = dedupe_entity_files_parallel(['E1'])"


class MiniUwazi(EntityRepositoryPort, FileRepositoryPort):
    """A real in-memory Uwazi miniature: entity ROWS + file ROWS, JOINed on read.

    The ``test_cache_resilient_file_cycle.py`` miniature, re-declared
    self-contained and trimmed to what the gate tests exercise: a raw's
    ``documents``/``attachments`` are a runtime JOIN from the files
    "collection" by sharedId, every upload mints a FRESH file ``_id`` +
    storage ``filename`` (Uwazi never rewrites them), and deleting a file
    drops its row + bytes. Public dicts are the "collections", seeded
    directly — plain real state, no mocks.
    """

    def __init__(self) -> None:
        self.entity_rows: dict[str, dict[str, Any]] = {}
        self.file_rows: dict[str, dict[str, Any]] = {}
        self.bytes_store: dict[str, bytes] = {}
        self.uploads: list[tuple[str, str, str, str]] = []  # (kind, sharedId, originalname, content_type)
        self.deleted_file_ids: list[str] = []
        self._next_id: int = 0

    # --- entity rows (with the runtime JOIN) ---------------------------------

    @override
    async def get_raw_by_shared_id(self, shared_id: str, language: str | None = None) -> dict[str, Any]:
        if shared_id not in self.entity_rows:
            raise RuntimeError(f"entity not found: {shared_id}")
        raw = dict(self.entity_rows[shared_id])
        files = [row for row in self.file_rows.values() if row["sharedId"] == shared_id]
        raw["documents"] = [dict(row) for row in files if row["type"] == "document"]
        raw["attachments"] = [dict(row) for row in files if row["type"] == "attachment"]
        return raw

    @override
    async def get_raw_by_internal_id(self, internal_id: str) -> dict[str, Any]:
        raise NotImplementedError("the gate tests never read by internal id")

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

    Like the real :class:`FilesystemBackupStore`, ``clear_run`` wipes the
    run's snapshots + file bytes but KEEPS the manifest. ``clear_run_calls``
    is plain real state (not a mock): the refused-execute test asserts the
    refusal path never reaches it.
    """

    def __init__(self) -> None:
        self._manifests: dict[str, MigrationManifest] = {}
        self._snapshots: dict[str, dict[str, Any]] = {}
        self._file_bytes: dict[tuple[str, str, str], bytes] = {}
        self.clear_run_calls: list[str] = []

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
        self.clear_run_calls.append(run_id)
        for key in list(self._file_bytes):
            if key[0] == run_id:
                self._file_bytes.pop(key)
        self._snapshots.pop(run_id, None)

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
    for file_id, name, filename, data in docs + attachments:
        uwazi.file_rows[file_id] = {
            "_id": file_id,
            "sharedId": "E1",
            "type": "document" if file_id.startswith("d") else "attachment",
            "originalname": name,
            "filename": filename,
            "size": len(data),
        }
        uwazi.bytes_store[filename] = data


# --- wiring helpers (real ports throughout) --------------------------------------


def _wired() -> tuple[MiniUwazi, InMemoryBackupStore]:
    """The fully wired pair: a seeded instance + a store holding a PLANNED run."""
    uwazi = MiniUwazi()
    _seed_incident(uwazi)
    store = InMemoryBackupStore()
    store.save_manifest(
        "run-1",
        MigrationManifest(
            run_id="run-1",
            created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
            prompt="dedupe the incident entity",
            script=SCRIPT,
            status=RunStatus.PLANNED,
        ),
    )
    return uwazi, store


def _use_case(uwazi: MiniUwazi, store: InMemoryBackupStore) -> ExecuteScriptUseCase:
    """The real execute use case over the miniature (the production wiring minus
    the optional audit/cache ports, which default to None)."""
    return ExecuteScriptUseCase(
        entity_api=None,  # type: ignore[arg-type]  # a dedupe-only script never calls the CRUD helpers
        relationship_api=None,
        entity_repository=uwazi,
        backup_store=store,
        file_repository=uwazi,
    )


def _revert_use_case(uwazi: MiniUwazi, store: InMemoryBackupStore) -> RevertRunUseCase:
    return RevertRunUseCase(entity_repository=uwazi, backup_store=store, file_repository=uwazi)


# --- a refused consecutive execute clears NOTHING (the run stays revertable) -----


async def test_refused_consecutive_execute_clears_nothing_and_stays_revertable() -> None:
    """THE hole, closed: executing an EXECUTED-not-reverted run is refused, and
    the refusal leaves the manifest + backup bytes + live state exactly as
    pass 1 left them — proven by reverting right after and seeing both files
    come back."""
    uwazi, store = _wired()
    use_case = _use_case(uwazi, store)

    # Pass 1 — the real execute (PLANNED): deletes the redundant copies and
    # backs up their bytes (the run's only path back).
    manifest = await use_case.execute(SCRIPT, store.load_manifest("run-1"), run_id="run-1")
    assert manifest.status == RunStatus.EXECUTED
    assert [r.file_id for r in manifest.deleted_files] == ["d2", "h2"]
    assert uwazi.deleted_file_ids == ["d2", "h2"]
    assert store.load_file_bytes("run-1", "E1", "d2") == b"SPANISH"
    assert store.load_file_bytes("run-1", "E1", "h2") == b"HTML"

    # The consecutive attempt — the operator's report — is REFUSED.
    with pytest.raises(ExecuteRefusedError, match="revert"):
        await use_case.execute(SCRIPT, store.load_manifest("run-1"), run_id="run-1")

    # NOTHING was cleared: the touch set + backed-up bytes are intact, the
    # backup store was never wiped, and no second delete pass ran.
    manifest = store.load_manifest("run-1")
    assert manifest.status == RunStatus.EXECUTED
    assert [r.file_id for r in manifest.deleted_files] == ["d2", "h2"]
    assert store.load_file_bytes("run-1", "E1", "d2") == b"SPANISH"
    assert store.load_file_bytes("run-1", "E1", "h2") == b"HTML"
    assert store.clear_run_calls == []
    assert uwazi.deleted_file_ids == ["d2", "h2"]

    # Proof the run is STILL revertable: revert restores both deleted files.
    await _revert_use_case(uwazi, store).revert("run-1")
    assert store.load_manifest("run-1").status == RunStatus.REVERTED
    assert [u[:3] for u in uwazi.uploads] == [("document", "E1", "a.pdf"), ("attachment", "E1", "doc.html")]


# --- the REVERTED -> re-execute cycle keeps working end to end --------------------


async def test_revert_then_re_execute_resets_and_runs_again() -> None:
    """The operator's confirmed cycle (dedupe -> revert -> dedupe again) must
    keep working: the re-execute after revert is ALLOWED, the reset + clear
    fire, and the re-run rediscovers the restored duplicates (FRESH ids) and
    deletes them again — leaving a fresh, fully revertable touch set."""
    uwazi, store = _wired()
    use_case = _use_case(uwazi, store)

    # Pass 1 — execute, then revert: the restored copies come back fresh.
    manifest = await use_case.execute(SCRIPT, store.load_manifest("run-1"), run_id="run-1")
    assert manifest.status == RunStatus.EXECUTED
    await _revert_use_case(uwazi, store).revert("run-1")
    assert store.load_manifest("run-1").status == RunStatus.REVERTED
    assert [u[:3] for u in uwazi.uploads] == [("document", "E1", "a.pdf"), ("attachment", "E1", "doc.html")]

    # Pass 2 — re-execute after revert: allowed, with the reset.
    manifest = await use_case.execute(SCRIPT, store.load_manifest("run-1"), run_id="run-1")
    assert manifest.status == RunStatus.EXECUTED
    assert store.clear_run_calls == ["run-1"]  # the reset path fired exactly once
    # Pass 1's backed-up bytes were wiped by the reset...
    with pytest.raises(FileNotFoundError):
        store.load_file_bytes("run-1", "E1", "d2")
    # ...the re-run recorded ONLY the fresh copies it deleted this pass.
    assert [r.file_id for r in manifest.deleted_files] == ["n1", "n2"]
    assert uwazi.deleted_file_ids == ["d2", "h2", "n1", "n2"]
    assert store.load_file_bytes("run-1", "E1", "n1") == b"SPANISH"
    assert store.load_file_bytes("run-1", "E1", "n2") == b"HTML"

    # And pass 2's own deletes are revertable too (the cycle never degrades).
    await _revert_use_case(uwazi, store).revert("run-1")
    assert store.load_manifest("run-1").status == RunStatus.REVERTED
    assert [u[:3] for u in uwazi.uploads] == [
        ("document", "E1", "a.pdf"),
        ("attachment", "E1", "doc.html"),
        ("document", "E1", "a.pdf"),
        ("attachment", "E1", "doc.html"),
    ]
