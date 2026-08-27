from datetime import datetime, timezone
from typing import Any, override

import pytest

from uwazi_admin_agent.domain.manifest import EntityIdentity, MigrationManifest, RewiredRelationship, RunStatus
from uwazi_admin_agent.domain.revert_gate import RevertRefusedError
from uwazi_admin_agent.domain.snapshot import EntitySnapshot, FileRef
from uwazi_admin_agent.ports.backup_store_port import BackupStorePort
from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort
from uwazi_admin_agent.ports.file_repository_port import FileRepositoryPort
from uwazi_admin_agent.ports.template_property_port import TemplatePropertyLookupPort
from uwazi_admin_agent.use_cases.revert_run_use_case import RevertRunUseCase

pytestmark = pytest.mark.anyio


# --- in-memory ports (real classes, not mocks) ------------------------------


class InMemoryEntityRepository(EntityRepositoryPort):
    """Stores raw entity dicts keyed by sharedId (the raw Uwazi JSON key).

    ``create_raw`` models the Uwazi create branch: it mints a fresh sharedId and
    stores a copy of the payload (without identity) under that new key, returning
    it so the caller can record/track the re-created entity.
    """

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
        for raw in self._entities.values():
            if raw.get("_id") == internal_id:
                return dict(raw)
        raise RuntimeError(f"Entity not found by _id: {internal_id}")

    @override
    async def save_raw(self, raw: dict[str, Any]) -> None:
        sid = raw.get("sharedId")
        if sid is None:
            raise RuntimeError("raw missing sharedId")
        self._entities[sid] = dict(raw)
        self.save_calls.append(dict(raw))

    @override
    async def create_raw(self, raw: dict[str, Any]) -> str:
        # Mirror Uwazi create: drop identity, mint a fresh sharedId, store a copy
        # under the new id (data fields preserved).
        self._next_id += 1
        new_sid = f"new-{self._next_id}"
        stored = {k: v for k, v in raw.items() if k not in {"_id", "sharedId"}}
        stored["sharedId"] = new_sid
        self._entities[new_sid] = stored
        return new_sid

    @override
    async def delete_by_shared_id(self, shared_id: str) -> None:
        self._entities.pop(shared_id, None)

    def has(self, shared_id: str) -> bool:
        return shared_id in self._entities

    def get(self, shared_id: str) -> dict[str, Any]:
        return self._entities[shared_id]


class InMemoryBackupStore(BackupStorePort):
    """Stores manifests, snapshots, and captured file bytes in plain dicts."""

    def __init__(self) -> None:
        self._manifests: dict[str, MigrationManifest] = {}
        self._snapshots: dict[str, dict[str, EntitySnapshot]] = {}
        self._file_bytes: dict[tuple[str, str, str], bytes] = {}

    @override
    def save_snapshot(self, run_id: str, snapshot: EntitySnapshot) -> None:
        self._snapshots.setdefault(run_id, {})[snapshot.shared_id] = snapshot

    @override
    def load_snapshot(self, run_id: str, shared_id: str) -> EntitySnapshot:
        if run_id not in self._snapshots or shared_id not in self._snapshots[run_id]:
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
        if run_id not in self._manifests:
            raise FileNotFoundError(f"No manifest for run={run_id}")
        return self._manifests[run_id]

    @override
    def update_status(self, run_id: str, status: RunStatus) -> None:
        self._manifests[run_id].status = status

    @override
    def clear_run(self, run_id: str) -> None:
        self._snapshots.pop(run_id, None)
        for key in list(self._file_bytes):
            if key[0] == run_id:
                self._file_bytes.pop(key)

    @override
    def list_runs(self) -> list[str]:
        return sorted(self._manifests.keys())

    @override
    def delete_run(self, run_id: str) -> None:
        self._manifests.pop(run_id, None)
        self._snapshots.pop(run_id, None)
        for key in list(self._file_bytes):
            if key[0] == run_id:
                self._file_bytes.pop(key)

    @override
    def rename_run(self, old_id: str, new_id: str) -> None:
        if old_id not in self._manifests:
            raise FileNotFoundError(old_id)
        if new_id in self._manifests:
            raise FileExistsError(new_id)
        self._manifests[new_id] = self._manifests.pop(old_id)
        self._manifests[new_id].run_id = new_id
        if old_id in self._snapshots:
            self._snapshots[new_id] = self._snapshots.pop(old_id)


class InMemoryFileRepository(FileRepositoryPort):
    """Records uploads and serves captured bytes back; models Uwazi's upload endpoints.

    Each upload appends a file descriptor to the target entity's document/attachment
    list (keyed by new sharedId) so verification can observe the re-uploaded files.
    ``failing`` turns every upload into a failure to exercise the best-effort path.
    """

    def __init__(self, failing: bool = False) -> None:
        self.uploads: list[tuple[str, str, str, str]] = []  # (kind, shared_id, originalname, content_type)
        self.failing: bool = failing

    @override
    async def get_file_bytes(self, filename: str) -> bytes | None:
        return None  # not used by revert (bytes come from the backup store)

    @override
    async def upload_document(
        self, data: bytes, shared_id: str, language: str | None, title: str, content_type: str
    ) -> bool:
        if self.failing:
            return False
        self.uploads.append(("document", shared_id, title, content_type))
        return True

    @override
    async def upload_attachment(
        self, data: bytes, shared_id: str, language: str | None, title: str, content_type: str
    ) -> bool:
        if self.failing:
            return False
        self.uploads.append(("attachment", shared_id, title, content_type))
        return True


class InMemoryTemplatePropertyLookup(TemplatePropertyLookupPort):
    """Resolves a relationship property name from a literal ``(template, type)`` map.

    ``mapping`` keys ``(template_id, relation_type_id)`` to a property ``name``.
    ``unresolved`` makes every lookup return ``None`` to exercise the best-effort
    inbound-skip path. ``calls`` records lookups so tests can assert resolution
    was attempted with the right ``(template, relation_type, content)``.
    """

    def __init__(
        self,
        mapping: dict[tuple[str, str], str] | None = None,
        unresolved: bool = False,
    ) -> None:
        self.mapping: dict[tuple[str, str], str] = mapping or {}
        self.unresolved: bool = unresolved
        self.calls: list[tuple[str, str, str | None]] = []

    @override
    async def find_relationship_property_name(
        self, template_id: str, relation_type_id: str, content_id: str | None = None
    ) -> str | None:
        self.calls.append((template_id, relation_type_id, content_id))
        if self.unresolved:
            return None
        return self.mapping.get((template_id, relation_type_id))


# --- helpers ----------------------------------------------------------------


def _now() -> datetime:
    return datetime(2024, 1, 1, tzinfo=timezone.utc)


def _identity(shared_id: str, language: str | None = None) -> EntityIdentity:
    return EntityIdentity(shared_id=shared_id, language=language)


def _snapshot(shared_id: str, raw: dict[str, Any], files: list[FileRef] | None = None) -> EntitySnapshot:
    return EntitySnapshot(
        shared_id=shared_id,
        internal_id=raw.get("_id"),
        language=raw.get("language"),
        raw=raw,
        captured_at=_now(),
        files=files,
    )


def _manifest(
    modified: list[EntityIdentity] | None = None,
    rewired: list[RewiredRelationship] | None = None,
    created: list[EntityIdentity] | None = None,
    deleted: list[EntityIdentity] | None = None,
) -> MigrationManifest:
    return MigrationManifest(
        run_id="run-1",
        created_at=_now(),
        prompt="d",
        script="x = 1",
        modified=modified or [],
        rewired=rewired or [],
        created=created or [],
        deleted=deleted or [],
        status=RunStatus.EXECUTED,
    )


# --- revert modified entities ----------------------------------------------


async def test_revert_modified_restores_from_snapshot() -> None:
    repo = InMemoryEntityRepository(
        entities={
            "A": {"sharedId": "A", "_id": "a1", "title": "CHANGED", "language": "en"},
            "B": {"sharedId": "B", "_id": "b1", "title": "CHANGED B", "language": "en"},
        }
    )
    store = InMemoryBackupStore()
    store.save_snapshot("run-1", _snapshot("A", {"sharedId": "A", "_id": "a1", "title": "old A", "language": "en"}))
    store.save_snapshot("run-1", _snapshot("B", {"sharedId": "B", "_id": "b1", "title": "old B", "language": "en"}))
    store.save_manifest("run-1", _manifest(modified=[_identity("A"), _identity("B")]))

    use_case = RevertRunUseCase(entity_repository=repo, backup_store=store)
    await use_case.revert("run-1")

    assert repo.get("A")["title"] == "old A"
    assert repo.get("B")["title"] == "old B"
    assert store.load_manifest("run-1").status == RunStatus.REVERTED


# --- revert deleted entities (re-create from snapshot with a NEW sharedId) ------


async def test_revert_deleted_recreates_from_snapshot() -> None:
    repo = InMemoryEntityRepository(entities={})  # entity was deleted
    store = InMemoryBackupStore()
    store.save_snapshot(
        "run-1",
        _snapshot("X", {"sharedId": "X", "_id": "x1", "title": "old X", "language": "en"}),
    )
    store.save_manifest("run-1", _manifest(deleted=[_identity("X")]))

    use_case = RevertRunUseCase(entity_repository=repo, backup_store=store)
    await use_case.revert("run-1")

    # Re-created under a NEW sharedId (the old id is gone), data preserved.
    assert not repo.has("X")
    new_ids = [sid for sid in ("new-1",) if repo.has(sid)]
    assert new_ids, "re-created entity should be present under a new sharedId"
    assert repo.get("new-1")["title"] == "old X"
    # The manifest records the restored (new) sharedId for verification + audit.
    deleted_entry = store.load_manifest("run-1").deleted[0]
    assert deleted_entry.restored_shared_id == "new-1"
    assert store.load_manifest("run-1").status == RunStatus.REVERTED


# --- revert created entities (delete them) ---------------------------------


async def test_revert_created_deletes_them() -> None:
    repo = InMemoryEntityRepository(
        entities={
            "NEW1": {"sharedId": "NEW1", "_id": "n1", "title": "created", "language": "en"},
            "NEW2": {"sharedId": "NEW2", "_id": "n2", "title": "created 2", "language": "en"},
        }
    )
    store = InMemoryBackupStore()
    store.save_manifest("run-1", _manifest(created=[_identity("NEW1"), _identity("NEW2")]))

    use_case = RevertRunUseCase(entity_repository=repo, backup_store=store)
    await use_case.revert("run-1")

    assert not repo.has("NEW1")
    assert not repo.has("NEW2")
    assert store.load_manifest("run-1").status == RunStatus.REVERTED


# --- revert rewired relationships (patch relations field) -------------------


async def test_revert_rewired_patches_relations() -> None:
    before_relations = [{"_id": "r1", "label": "old"}]
    repo = InMemoryEntityRepository(
        entities={
            "R": {"sharedId": "R", "_id": "r1", "relations": [{"_id": "r2", "label": "new"}], "language": "en"},
        }
    )
    store = InMemoryBackupStore()
    store.save_manifest(
        "run-1",
        _manifest(
            rewired=[
                RewiredRelationship(
                    entity=_identity("R", language="en"),
                    property_name="relations",
                    before=before_relations,
                )
            ],
        ),
    )

    use_case = RevertRunUseCase(entity_repository=repo, backup_store=store)
    await use_case.revert("run-1")

    assert repo.get("R")["relations"] == before_relations
    assert store.load_manifest("run-1").status == RunStatus.REVERTED


# --- full ordering: relationships → modified → deleted → created -----------


async def test_full_revert_ordering() -> None:
    before_relations: list[dict[str, Any]] = []
    repo = InMemoryEntityRepository(
        entities={
            "M": {"sharedId": "M", "_id": "m1", "title": "changed M", "relations": [{"_id": "rx"}], "language": "en"},
            "C": {"sharedId": "C", "_id": "c1", "title": "created C", "language": "en"},
        }
    )
    # "D" was deleted by the script, so it is absent from the repo.

    store = InMemoryBackupStore()
    store.save_snapshot(
        "run-1",
        _snapshot("M", {"sharedId": "M", "_id": "m1", "title": "old M", "relations": before_relations, "language": "en"}),
    )
    store.save_snapshot("run-1", _snapshot("D", {"sharedId": "D", "_id": "d1", "title": "old D", "language": "en"}))
    store.save_manifest(
        "run-1",
        _manifest(
            modified=[_identity("M")],
            rewired=[
                RewiredRelationship(
                    entity=_identity("M", language="en"),
                    property_name="relations",
                    before=before_relations,
                )
            ],
            created=[_identity("C")],
            deleted=[_identity("D")],
        ),
    )

    use_case = RevertRunUseCase(entity_repository=repo, backup_store=store)
    await use_case.revert("run-1")

    # M restored to old state (including relations)
    assert repo.get("M")["title"] == "old M"
    assert repo.get("M")["relations"] == before_relations
    # D re-created from snapshot under a NEW sharedId (old id is gone)
    assert not repo.has("D")
    assert repo.has("new-1")
    assert repo.get("new-1")["title"] == "old D"
    assert store.load_manifest("run-1").deleted[0].restored_shared_id == "new-1"
    # C deleted
    assert not repo.has("C")
    assert store.load_manifest("run-1").status == RunStatus.REVERTED


# --- empty manifest --------------------------------------------------------


async def test_revert_empty_manifest_updates_status() -> None:
    repo = InMemoryEntityRepository(entities={})
    store = InMemoryBackupStore()
    store.save_manifest("run-1", _manifest())

    use_case = RevertRunUseCase(entity_repository=repo, backup_store=store)
    await use_case.revert("run-1")

    assert store.load_manifest("run-1").status == RunStatus.REVERTED


# --- revert gate: refuse an already-REVERTED run ----------------------------


async def test_revert_refuses_already_reverted_run() -> None:
    repo = InMemoryEntityRepository(entities={})
    store = InMemoryBackupStore()
    store.save_manifest("run-1", _manifest())  # status defaults to EXECUTED here
    store.update_status("run-1", RunStatus.REVERTED)

    use_case = RevertRunUseCase(entity_repository=repo, backup_store=store)
    with pytest.raises(RevertRefusedError):
        await use_case.revert("run-1")


# --- delete-revert file restore (re-upload captured files to the new sharedId) ---


def _file_ref(
    file_id: str, kind: str, originalname: str, language: str | None = "en", content_type: str | None = None
) -> FileRef:
    return FileRef(
        file_id=file_id,
        kind=kind,  # type: ignore[arg-type]
        filename=f"storage-{file_id}",
        originalname=originalname,
        language=language,
        content_type=content_type or ("application/pdf" if kind == "document" else "image/png"),
    )


async def test_revert_deleted_reuploads_files_to_new_shared_id() -> None:
    repo = InMemoryEntityRepository(entities={})  # entity was deleted
    store = InMemoryBackupStore()
    files = [
        _file_ref("fdoc1", "document", "report.pdf"),
        _file_ref("fatt1", "attachment", "scan.png"),
    ]
    raw = {"sharedId": "X", "_id": "x1", "title": "old X", "language": "en", "documents": [], "attachments": []}
    store.save_snapshot("run-1", _snapshot("X", raw, files=files))
    store.save_file_bytes("run-1", "X", "fdoc1", b"PDF-BYTES")
    store.save_file_bytes("run-1", "X", "fatt1", b"PNG-BYTES")
    store.save_manifest("run-1", _manifest(deleted=[_identity("X")]))

    file_repo = InMemoryFileRepository()
    use_case = RevertRunUseCase(entity_repository=repo, backup_store=store, file_repository=file_repo)
    await use_case.revert("run-1")

    # Entity re-created under a new sharedId with its data.
    assert repo.has("new-1")
    assert repo.get("new-1")["title"] == "old X"
    # Files re-uploaded to the NEW sharedId, documents first then attachments.
    assert file_repo.uploads == [
        ("document", "new-1", "report.pdf", "application/pdf"),
        ("attachment", "new-1", "scan.png", "image/png"),
    ]
    assert store.load_manifest("run-1").status == RunStatus.REVERTED


async def test_revert_deleted_without_file_repository_skips_file_restore() -> None:
    repo = InMemoryEntityRepository(entities={})
    store = InMemoryBackupStore()
    files = [_file_ref("fdoc1", "document", "report.pdf")]
    store.save_snapshot(
        "run-1", _snapshot("X", {"sharedId": "X", "_id": "x1", "title": "old X", "language": "en"}, files=files)
    )
    store.save_file_bytes("run-1", "X", "fdoc1", b"PDF-BYTES")
    store.save_manifest("run-1", _manifest(deleted=[_identity("X")]))

    # No file_repository injected — file restore is a no-op, revert still succeeds.
    use_case = RevertRunUseCase(entity_repository=repo, backup_store=store)
    await use_case.revert("run-1")

    assert repo.has("new-1")
    assert repo.get("new-1")["title"] == "old X"
    assert store.load_manifest("run-1").status == RunStatus.REVERTED


async def test_revert_deleted_file_upload_failure_is_best_effort() -> None:
    repo = InMemoryEntityRepository(entities={})
    store = InMemoryBackupStore()
    files = [_file_ref("fdoc1", "document", "report.pdf"), _file_ref("fatt1", "attachment", "scan.png")]
    store.save_snapshot(
        "run-1", _snapshot("X", {"sharedId": "X", "_id": "x1", "title": "old X", "language": "en"}, files=files)
    )
    store.save_file_bytes("run-1", "X", "fdoc1", b"PDF-BYTES")
    store.save_file_bytes("run-1", "X", "fatt1", b"PNG-BYTES")
    store.save_manifest("run-1", _manifest(deleted=[_identity("X")]))

    file_repo = InMemoryFileRepository(failing=True)
    use_case = RevertRunUseCase(entity_repository=repo, backup_store=store, file_repository=file_repo)
    # Revert must NOT raise even though every upload fails.
    await use_case.revert("run-1")

    # Entity is still re-created with its data; file restore flagged failed but reverted.
    assert repo.has("new-1")
    assert repo.get("new-1")["title"] == "old X"
    assert store.load_manifest("run-1").status == RunStatus.REVERTED
    # Both uploads were attempted (and both failed).
    assert len(file_repo.uploads) == 0


async def test_revert_deleted_missing_file_bytes_is_best_effort() -> None:
    repo = InMemoryEntityRepository(entities={})
    store = InMemoryBackupStore()
    files = [_file_ref("fdoc1", "document", "report.pdf")]
    # Snapshot claims a file was captured, but no bytes were stored (e.g. capture failed).
    store.save_snapshot(
        "run-1", _snapshot("X", {"sharedId": "X", "_id": "x1", "title": "old X", "language": "en"}, files=files)
    )
    store.save_manifest("run-1", _manifest(deleted=[_identity("X")]))

    file_repo = InMemoryFileRepository()
    use_case = RevertRunUseCase(entity_repository=repo, backup_store=store, file_repository=file_repo)
    await use_case.revert("run-1")

    # Entity re-created; the missing-bytes file was skipped, not fatal.
    assert repo.has("new-1")
    assert repo.get("new-1")["title"] == "old X"
    assert file_repo.uploads == []
    assert store.load_manifest("run-1").status == RunStatus.REVERTED


# --- delete-revert: strip co-deleted refs + re-apply relationship refs --------


def _rel(entity: str, hub: str, template: str | None) -> dict:
    return {"entity": entity, "hub": hub, "template": template}


def _mutual_snapshots() -> tuple[EntitySnapshot, EntitySnapshot]:
    snap_a = _snapshot(
        "A",
        {
            "sharedId": "A",
            "_id": "a1",
            "title": "A",
            "language": "en",
            "metadata": {"entity_relation": [{"value": "B", "label": "B"}]},
            "relations": [_rel("A", "h1", None), _rel("B", "h1", "rtype1")],
        },
    )
    snap_b = _snapshot(
        "B",
        {
            "sharedId": "B",
            "_id": "b1",
            "title": "B",
            "language": "en",
            "metadata": {"entity_relation": [{"value": "A", "label": "A"}]},
            "relations": [_rel("A", "h1", None), _rel("B", "h1", "rtype1")],
        },
    )
    return snap_a, snap_b


async def test_revert_deleted_mutual_strips_refs_then_reapplies_remapped() -> None:
    repo = InMemoryEntityRepository(entities={})  # both deleted
    store = InMemoryBackupStore()
    snap_a, snap_b = _mutual_snapshots()
    store.save_snapshot("run-1", snap_a)
    store.save_snapshot("run-1", snap_b)
    store.save_manifest("run-1", _manifest(deleted=[_identity("A", "en"), _identity("B", "en")]))

    use_case = RevertRunUseCase(entity_repository=repo, backup_store=store)
    await use_case.revert("run-1")

    # Both re-created under fresh sharedIds (A -> new-1, B -> new-2 in call order).
    assert repo.has("new-1") and repo.has("new-2")
    # The co-deleted metadata refs were STRIPPED before create (so create did not
    # 400), then RE-APPLIED remapped to the NEW sharedIds via the entity-save
    # path (no self-refs): new-1 -> new-2, new-2 -> new-1.
    assert repo.get("new-1")["metadata"]["entity_relation"] == [{"value": "new-2", "label": "B"}]
    assert repo.get("new-2")["metadata"]["entity_relation"] == [{"value": "new-1", "label": "A"}]
    # Snapshot raw is untouched (raw fidelity): the original ref is still there.
    assert store.load_snapshot("run-1", "A").raw["metadata"]["entity_relation"] == [{"value": "B", "label": "B"}]
    # restored_shared_id recorded for verification.
    assert {e.restored_shared_id for e in store.load_manifest("run-1").deleted} == {"new-1", "new-2"}
    assert store.load_manifest("run-1").status == RunStatus.REVERTED


async def test_revert_deleted_mutual_reapply_does_not_need_template_lookup() -> None:
    # The mutual re-apply (Part 2) re-saves re-created entities with remapped
    # metadata; it does NOT need the template-property lookup (that is only for
    # inbound refs on still-existing entities). So it works with no lookup wired.
    repo = InMemoryEntityRepository(entities={})
    store = InMemoryBackupStore()
    snap_a, snap_b = _mutual_snapshots()
    store.save_snapshot("run-1", snap_a)
    store.save_snapshot("run-1", snap_b)
    store.save_manifest("run-1", _manifest(deleted=[_identity("A", "en"), _identity("B", "en")]))

    use_case = RevertRunUseCase(entity_repository=repo, backup_store=store)
    await use_case.revert("run-1")

    assert repo.get("new-1")["metadata"]["entity_relation"] == [{"value": "new-2", "label": "B"}]
    assert repo.get("new-2")["metadata"]["entity_relation"] == [{"value": "new-1", "label": "A"}]
    assert store.load_manifest("run-1").status == RunStatus.REVERTED


async def test_revert_inbound_ref_on_still_existing_entity_is_restored() -> None:
    # Defect 1: A deleted (re-created new-1); B still-existing had B->A, which the
    # delete cascade stripped (B.metadata.entity_relation == []). Revert re-adds
    # the ref on B remapped to new-1, using the template lookup to resolve the
    # property name on B's template.
    repo = InMemoryEntityRepository(
        entities={
            "B": {
                "sharedId": "B",
                "_id": "b1",
                "title": "B",
                "language": "en",
                "template": "tmplB",
                "metadata": {"entity_relation": []},
            }
        }
    )
    store = InMemoryBackupStore()
    store.save_snapshot(
        "run-1",
        _snapshot(
            "A",
            {
                "sharedId": "A",
                "_id": "a1",
                "title": "A",
                "language": "en",
                "template": "tmplA",
                "metadata": {"entity_relation": [{"value": "B", "label": "B"}]},
                "relations": [
                    _rel("A", "h2", None),
                    _rel("B", "h2", "rtype1"),
                    _rel("B", "h1", None),
                    _rel("A", "h1", "rtype1"),
                ],
            },
        ),
    )
    store.save_manifest("run-1", _manifest(deleted=[_identity("A", "en")]))

    lookup = InMemoryTemplatePropertyLookup(mapping={("tmplB", "rtype1"): "entity_relation"})
    use_case = RevertRunUseCase(entity_repository=repo, backup_store=store, template_property_lookup=lookup)
    await use_case.revert("run-1")

    # A re-created; B's cascade-stripped ref restored pointing at new-1 (B's other
    # metadata untouched — only the entity_relation entry is appended).
    assert repo.has("new-1")
    assert repo.get("B")["metadata"]["entity_relation"] == [{"value": "new-1", "label": "A"}]
    assert lookup.calls == [("tmplB", "rtype1", "tmplA")]
    assert store.load_manifest("run-1").status == RunStatus.REVERTED


async def test_revert_inbound_ref_skipped_best_effort_when_property_unresolved() -> None:
    # The template lookup cannot resolve the property name (e.g. the existing
    # entity's template has no matching relationship property). The inbound ref
    # is skipped (best-effort); the revert still succeeds and A is re-created.
    repo = InMemoryEntityRepository(
        entities={
            "B": {
                "sharedId": "B",
                "_id": "b1",
                "title": "B",
                "language": "en",
                "template": "tmplB",
                "metadata": {"entity_relation": []},
            }
        }
    )
    store = InMemoryBackupStore()
    store.save_snapshot(
        "run-1",
        _snapshot(
            "A",
            {
                "sharedId": "A",
                "_id": "a1",
                "title": "A",
                "language": "en",
                "template": "tmplA",
                "metadata": {"entity_relation": [{"value": "B", "label": "B"}]},
                "relations": [_rel("B", "h1", None), _rel("A", "h1", "rtype1")],
            },
        ),
    )
    store.save_manifest("run-1", _manifest(deleted=[_identity("A", "en")]))

    lookup = InMemoryTemplatePropertyLookup(unresolved=True)
    use_case = RevertRunUseCase(entity_repository=repo, backup_store=store, template_property_lookup=lookup)
    await use_case.revert("run-1")

    assert repo.has("new-1")
    # B's ref was NOT restored (lookup unresolved) — best-effort, not fatal.
    assert repo.get("B")["metadata"]["entity_relation"] == []
    assert store.load_manifest("run-1").status == RunStatus.REVERTED


async def test_revert_deleted_self_ref_is_stripped_then_reapplied_remapped() -> None:
    # A references itself (a self-ref by its own sharedId) — must be stripped
    # before create so the create branch does not 400 on the not-yet-existing old
    # id, then re-applied remapped to the NEW sharedId (A' -> A'), mirroring the
    # original self-ref as exact-data revert.
    repo = InMemoryEntityRepository(entities={})
    store = InMemoryBackupStore()
    store.save_snapshot(
        "run-1",
        _snapshot(
            "A",
            {
                "sharedId": "A",
                "_id": "a1",
                "title": "A",
                "language": "en",
                "metadata": {"entity_relation": [{"value": "A", "label": "A"}]},
            },
        ),
    )
    store.save_manifest("run-1", _manifest(deleted=[_identity("A", "en")]))

    use_case = RevertRunUseCase(entity_repository=repo, backup_store=store)
    await use_case.revert("run-1")

    assert repo.has("new-1")
    assert repo.get("new-1")["metadata"]["entity_relation"] == [{"value": "new-1", "label": "A"}]
    assert store.load_snapshot("run-1", "A").raw["metadata"]["entity_relation"] == [{"value": "A", "label": "A"}]
