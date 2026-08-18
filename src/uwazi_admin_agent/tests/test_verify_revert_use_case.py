"""Isolated unit tests for :class:`VerifyRevertUseCase` (Phase 6 DoD).

No mocks, no network — tiny real in-memory ports (the AGENTS.md-sanctioned
pattern from ``test_revert_run_use_case.py``) + literal snapshots + plain
assertions. A "simulated mismatch" (the DoD's "verification catches a simulated
mismatch") is produced by handing the in-memory repo a *wrong* current raw.
"""

from datetime import datetime, timezone
from typing import Any, override

import pytest

from uwazi_admin_agent.domain.manifest import EntityIdentity, MigrationManifest, RunStatus
from uwazi_admin_agent.domain.snapshot import EntitySnapshot
from uwazi_admin_agent.ports.backup_store_port import BackupStorePort
from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort
from uwazi_admin_agent.use_cases.verify_revert_use_case import VerifyRevertUseCase

pytestmark = pytest.mark.anyio


# --- in-memory ports (real classes, not mocks) ------------------------------


class InMemoryEntityRepository(EntityRepositoryPort):
    """Stores raw entity dicts keyed by sharedId. Raises on a missing id."""

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


class InMemoryBackupStore(BackupStorePort):
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
    deleted: list[EntityIdentity] | None = None,
    created: list[EntityIdentity] | None = None,
) -> MigrationManifest:
    return MigrationManifest(
        run_id="run-1",
        created_at=_now(),
        prompt="d",
        script="x = 1",
        modified=modified or [],
        deleted=deleted or [],
        created=created or [],
        status=RunStatus.REVERTED,
    )


# --- ok cases -------------------------------------------------------------


async def test_verify_modified_matching_snapshot_is_ok() -> None:
    repo = InMemoryEntityRepository(entities={"A": {"sharedId": "A", "_id": "a1", "title": "old", "language": "en"}})
    store = InMemoryBackupStore()
    store.save_snapshot("run-1", _snapshot("A", {"sharedId": "A", "_id": "a1", "title": "old", "language": "en"}))
    store.save_manifest("run-1", _manifest(modified=[EntityIdentity(shared_id="A")]))

    use_case = VerifyRevertUseCase(entity_repository=repo, backup_store=store)
    result = await use_case.verify("run-1")

    assert result.ok is True
    assert result.checked == 1
    assert result.mismatches == []


async def test_verify_empty_manifest_is_ok() -> None:
    repo = InMemoryEntityRepository(entities={})
    store = InMemoryBackupStore()
    store.save_manifest("run-1", _manifest())

    use_case = VerifyRevertUseCase(entity_repository=repo, backup_store=store)
    result = await use_case.verify("run-1")

    assert result.ok is True
    assert result.checked == 0


async def test_verify_created_gone_is_ok() -> None:
    # The created entity was deleted by revert; the repo has no record of it.
    repo = InMemoryEntityRepository(entities={})
    store = InMemoryBackupStore()
    store.save_manifest("run-1", _manifest(created=[EntityIdentity(shared_id="C")]))

    use_case = VerifyRevertUseCase(entity_repository=repo, backup_store=store)
    result = await use_case.verify("run-1")

    assert result.ok is True
    assert result.checked == 1


# --- mismatch cases (the DoD's "verification catches a simulated mismatch") ---


async def test_verify_catches_modified_data_field_mismatch() -> None:
    # Simulated mismatch: the revert restored the WRONG title.
    repo = InMemoryEntityRepository(entities={"A": {"sharedId": "A", "_id": "a1", "title": "WRONG", "language": "en"}})
    store = InMemoryBackupStore()
    store.save_snapshot("run-1", _snapshot("A", {"sharedId": "A", "_id": "a1", "title": "old", "language": "en"}))
    store.save_manifest("run-1", _manifest(modified=[EntityIdentity(shared_id="A")]))

    use_case = VerifyRevertUseCase(entity_repository=repo, backup_store=store)
    result = await use_case.verify("run-1")

    assert result.ok is False
    assert len(result.mismatches) == 1
    assert result.mismatches[0].shared_id == "A"
    assert result.mismatches[0].kind == "entity"


async def test_verify_catches_created_entity_still_present() -> None:
    # The created entity should have been deleted by the revert but is still
    # present in the repo — the revert failed to delete it.
    repo = InMemoryEntityRepository(entities={"C": {"sharedId": "C", "_id": "c1", "title": "survived", "language": "en"}})
    store = InMemoryBackupStore()
    store.save_manifest("run-1", _manifest(created=[EntityIdentity(shared_id="C")]))

    use_case = VerifyRevertUseCase(entity_repository=repo, backup_store=store)
    result = await use_case.verify("run-1")

    assert result.ok is False
    assert result.mismatches[0].kind == "created"
    assert result.mismatches[0].shared_id == "C"


async def test_verify_catches_deleted_entity_not_recreated() -> None:
    # The deleted entity should have been re-created from its snapshot but the
    # repo is empty — revert failed to restore it.
    repo = InMemoryEntityRepository(entities={})
    store = InMemoryBackupStore()
    store.save_snapshot("run-1", _snapshot("D", {"sharedId": "D", "_id": "d1", "title": "old D", "language": "en"}))
    store.save_manifest("run-1", _manifest(deleted=[EntityIdentity(shared_id="D")]))

    use_case = VerifyRevertUseCase(entity_repository=repo, backup_store=store)
    result = await use_case.verify("run-1")

    assert result.ok is False
    assert result.mismatches[0].kind == "entity"
    assert result.mismatches[0].actual is None


# --- editDate exclusion flows through the use case -----------------------


async def test_verify_editdate_only_difference_is_ok() -> None:
    repo = InMemoryEntityRepository(
        entities={"A": {"sharedId": "A", "_id": "a1", "title": "old", "editDate": 1005, "language": "en"}}
    )
    store = InMemoryBackupStore()
    store.save_snapshot(
        "run-1", _snapshot("A", {"sharedId": "A", "_id": "a1", "title": "old", "editDate": 1000, "language": "en"})
    )
    store.save_manifest("run-1", _manifest(modified=[EntityIdentity(shared_id="A")]))

    use_case = VerifyRevertUseCase(entity_repository=repo, backup_store=store)
    result = await use_case.verify("run-1")

    assert result.ok is True
    assert result.mismatches == []
