from datetime import datetime, timezone
from typing import Any, override

import pytest

from uwazi_admin_agent.domain.manifest import EntityIdentity, MigrationManifest, RewiredRelationship, RunStatus
from uwazi_admin_agent.domain.snapshot import EntitySnapshot
from uwazi_admin_agent.ports.backup_store_port import BackupStorePort
from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort
from uwazi_admin_agent.use_cases.revert_run_use_case import RevertRunUseCase

pytestmark = pytest.mark.anyio


# --- in-memory ports (real classes, not mocks) ------------------------------


class InMemoryEntityRepository(EntityRepositoryPort):
    """Stores raw entity dicts keyed by sharedId (the raw Uwazi JSON key)."""

    def __init__(self, entities: dict[str, dict[str, Any]] | None = None) -> None:
        self._entities: dict[str, dict[str, Any]] = dict(entities or {})

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

    @override
    async def delete_by_shared_id(self, shared_id: str) -> None:
        self._entities.pop(shared_id, None)

    def has(self, shared_id: str) -> bool:
        return shared_id in self._entities

    def get(self, shared_id: str) -> dict[str, Any]:
        return self._entities[shared_id]


class InMemoryBackupStore(BackupStorePort):
    """Stores manifests and snapshots in plain dicts."""

    def __init__(self) -> None:
        self._manifests: dict[str, MigrationManifest] = {}
        self._snapshots: dict[str, dict[str, EntitySnapshot]] = {}

    @override
    def save_snapshot(self, run_id: str, snapshot: EntitySnapshot) -> None:
        self._snapshots.setdefault(run_id, {})[snapshot.shared_id] = snapshot

    @override
    def load_snapshot(self, run_id: str, shared_id: str) -> EntitySnapshot:
        if run_id not in self._snapshots or shared_id not in self._snapshots[run_id]:
            raise FileNotFoundError(f"No snapshot for run={run_id} sharedId={shared_id}")
        return self._snapshots[run_id][shared_id]

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
    def list_runs(self) -> list[str]:
        return sorted(self._manifests.keys())


# --- helpers ----------------------------------------------------------------


def _now() -> datetime:
    return datetime(2024, 1, 1, tzinfo=timezone.utc)


def _identity(shared_id: str, language: str | None = None) -> EntityIdentity:
    return EntityIdentity(shared_id=shared_id, language=language)


def _snapshot(shared_id: str, raw: dict[str, Any]) -> EntitySnapshot:
    return EntitySnapshot(
        shared_id=shared_id,
        internal_id=raw.get("_id"),
        language=raw.get("language"),
        raw=raw,
        captured_at=_now(),
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


# --- revert deleted entities (re-create from snapshot) ---------------------


async def test_revert_deleted_recreates_from_snapshot() -> None:
    repo = InMemoryEntityRepository(entities={})  # entity was deleted
    store = InMemoryBackupStore()
    store.save_snapshot("run-1", _snapshot("X", {"sharedId": "X", "_id": "x1", "title": "old X", "language": "en"}))
    store.save_manifest("run-1", _manifest(deleted=[_identity("X")]))

    use_case = RevertRunUseCase(entity_repository=repo, backup_store=store)
    await use_case.revert("run-1")

    assert repo.has("X")
    assert repo.get("X")["title"] == "old X"
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
    # D re-created from snapshot
    assert repo.has("D")
    assert repo.get("D")["title"] == "old D"
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
