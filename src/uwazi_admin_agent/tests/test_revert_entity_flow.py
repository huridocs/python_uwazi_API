"""Isolated tests for the per-entity multi-language revert fix (per AGENTS.md).

No network, no mocks: a real ``SqliteSuggestionStore`` over a tmp file and the
same in-memory ports the revert tests use. They pin the fix for the
partial-revert bug: the review table is one row per (entity, LANGUAGE), but the
writes under one sharedId share ONE first-touch backup — so reverting a row
restores EVERY captured language row of the entity, not just the clicked row's.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, override

import pytest

from uwazi_admin_agent.adapters.sqlite_suggestion_store import SqliteSuggestionStore
from uwazi_admin_agent.domain.manifest import EntityIdentity, MigrationManifest, RunStatus
from uwazi_admin_agent.domain.revert_gate import RevertRefusedError
from uwazi_admin_agent.domain.snapshot import EntityRowSnapshot, EntitySnapshot
from uwazi_admin_agent.drivers.web.review_view import entity_row, revert_entity_confirmation
from uwazi_admin_agent.ports.backup_store_port import BackupStorePort
from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort
from uwazi_admin_agent.use_cases.revert_run_use_case import RevertRunUseCase

pytestmark = pytest.mark.anyio


# --- in-memory ports (real classes, not mocks) -------------------------------


class InMemoryEntityRepository(EntityRepositoryPort):
    """Raw dict store keyed by sharedId, recording save language per call."""

    def __init__(self) -> None:
        self._entities: dict[str, dict[str, Any]] = {}
        self.saved: list[tuple[str, str]] = []  # (sharedId, language) per save

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
        language = raw.get("language")
        self.saved.append((sid, str(language or "")))

    @override
    async def create_raw(self, raw: dict[str, Any]) -> str:
        raise AssertionError("not used in this test")

    @override
    async def delete_by_shared_id(self, shared_id: str) -> None:
        self._entities.pop(shared_id, None)


class InMemoryBackupStore(BackupStorePort):
    """Plain-dict snapshot + manifest store."""

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
    def save_file_bytes(self, run_id: str, shared_id: str, file_id: str, data: bytes) -> None:
        raise AssertionError("not used")

    @override
    def load_file_bytes(self, run_id: str, shared_id: str, file_id: str) -> bytes:
        raise AssertionError("not used")

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
        return sorted(self._manifests)

    @override
    def clear_run(self, run_id: str) -> None:
        self._snapshots.pop(run_id, None)

    @override
    def delete_run(self, run_id: str) -> None:
        self._manifests.pop(run_id, None)
        self._snapshots.pop(run_id, None)

    @override
    def rename_run(self, old_id: str, new_id: str) -> None:
        raise AssertionError("not used")


def _snapshot(shared_id: str, captured: datetime) -> EntitySnapshot:
    es_row = {
        "_id": "row_es_id",
        "sharedId": shared_id,
        "language": "es",
        "title": "Antes",
        "metadata": {"excerpt": [{"value": "antes_es", "language": "es"}]},
    }
    en_row = {
        "_id": "row_en_id",
        "sharedId": shared_id,
        "language": "en",
        "title": "Before",
        "metadata": {"excerpt": [{"value": "before_en", "language": "en"}]},
    }
    return EntitySnapshot(
        shared_id=shared_id,
        internal_id="row_es_id",
        language="es",
        raw=es_row,
        captured_at=captured,
        rows=[
            EntityRowSnapshot(language="es", internal_id="row_es_id", raw=es_row),
            EntityRowSnapshot(language="en", internal_id="row_en_id", raw=en_row),
        ],
        affected_languages=["en", "es"],
    )


@pytest.fixture
def store(tmp_path: Any) -> SqliteSuggestionStore:
    db = SqliteSuggestionStore(tmp_path / "suggestions.db")
    yield db
    db.close()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def test_revert_entity_restores_every_language_row(store: SqliteSuggestionStore) -> None:
    """One per-row revert rolls the WHOLE entity back — both language rows."""
    repo = InMemoryEntityRepository()
    backups = InMemoryBackupStore()
    captured = datetime(2026, 2, 3, 10, 0, tzinfo=timezone.utc)
    backups.save_snapshot("run1", _snapshot("s1", captured))
    manifest = MigrationManifest(
        run_id="run1",
        created_at=captured,
        prompt="p",
        script="",
        modified=[EntityIdentity(shared_id="s1", language="es")],
    )
    backups.save_manifest("run1", manifest)
    use_case = RevertRunUseCase(entity_repository=repo, backup_store=backups)

    snapshot = await use_case.revert_entity("run1", "s1")

    assert snapshot is not None
    assert snapshot.captured_at == captured
    # The Uwazi-facing save happened once PER language row, in the row's own locale:
    # the write landed on the es row AND the en row (the partial-revert bug's fix).
    assert sorted(repo.saved) == [("s1", "en"), ("s1", "es")]
    saved_raws = [call for call in repo.saved]
    assert saved_raws[0] == ("s1", "es") and saved_raws[1] == ("s1", "en")


async def test_revert_entity_without_backup_returns_none() -> None:
    repo = InMemoryEntityRepository()
    backups = InMemoryBackupStore()
    backups.save_manifest(
        "run1",
        MigrationManifest(run_id="run1", created_at=datetime.now(timezone.utc), prompt="p", script=""),
    )
    use_case = RevertRunUseCase(entity_repository=repo, backup_store=backups)
    assert await use_case.revert_entity("run1", "never-written") is None
    assert repo.saved == []


async def test_revert_entity_refuses_created_entity() -> None:
    """A created entity's undo is a delete (run-level); the row revert refuses it."""
    repo = InMemoryEntityRepository()
    backups = InMemoryBackupStore()
    captured = datetime.now(timezone.utc)
    manifest = MigrationManifest(
        run_id="run1",
        created_at=captured,
        prompt="p",
        script="",
        created=[EntityIdentity(shared_id="made-1", language="en")],
    )
    backups.save_manifest("run1", manifest)
    use_case = RevertRunUseCase(entity_repository=repo, backup_store=backups)
    with pytest.raises(RevertRefusedError):
        await use_case.revert_entity("run1", "made-1")


async def test_revert_entity_flips_all_language_rows_of_the_entity(store: SqliteSuggestionStore) -> None:
    """The revert's status flip covers the entity's rows in EVERY language.

    The table is per (entity, language); one restore undoes both rows' writes,
    so both move APPLIED → REVERTED (the operator can re-run either).
    """
    store.add_suggestions(
        [
            _suggestion("run1", 1, "s1", "es", status="applied"),
            _suggestion("run1", 1, "s1", "en", status="applied"),
        ]
    )
    repo = InMemoryEntityRepository()
    backups = InMemoryBackupStore()
    captured = datetime(2026, 2, 3, 10, 0, tzinfo=timezone.utc)
    backups.save_snapshot("run1", _snapshot("s1", captured))
    backups.save_manifest(
        "run1",
        MigrationManifest(
            run_id="run1",
            created_at=captured,
            prompt="p",
            script="",
            modified=[EntityIdentity(shared_id="s1", language="es")],
        ),
    )
    use_case = RevertRunUseCase(entity_repository=repo, backup_store=backups, suggestion_store=store)

    await use_case.revert_entity("run1", "s1")

    rows, _total = store.entity_rows(
        "run1",
        1,
        __import__("uwazi_admin_agent.domain.suggestion", fromlist=["SuggestionFilter"]).SuggestionFilter(limit=100),
    )
    by_lang = {row.language: row for row in rows}
    assert by_lang["es"].reverted == 1
    assert by_lang["en"].reverted == 1
    assert all(row.applied == 0 for row in rows)


def _suggestion(run_id: str, attempt: int, shared_id: str, language: str, status: str) -> Any:
    from uwazi_admin_agent.domain.suggestion import Suggestion

    return Suggestion(
        run_id=run_id,
        attempt=attempt,
        shared_id=shared_id,
        op="update",
        language=language,
        property_name="excerpt",
        proposed_value="x",
        status=status,  # type: ignore[arg-type]
    )


# --- row contract ------------------------------------------------------------


def test_entity_row_carries_revert_tooltip_and_revertable_flag() -> None:
    from uwazi_admin_agent.domain.suggestion import EntityReviewRow

    row = entity_row(
        EntityReviewRow(
            shared_id="s1",
            language="es",
            ops=["update"],
            properties=["excerpt"],
            proposal_count=1,
            accepted=0,
            pending=0,
            applied=1,
            reverted=0,
            discarded=0,
            has_outcome=True,
            match_summary="exact",
        ),
        None,
        backup_created_at=datetime(2026, 2, 3, 10, 5, tzinfo=timezone.utc),
    )
    assert row["revertable"] is True
    assert row["revert_tooltip"] == "entity backup creation 2026-02-03 10:05"


def test_entity_row_pending_row_is_not_revertable() -> None:
    from uwazi_admin_agent.domain.suggestion import EntityReviewRow

    row = entity_row(
        EntityReviewRow(
            shared_id="s2",
            language=None,
            ops=["delete"],
            properties=[],
            proposal_count=2,
            accepted=0,
            pending=1,
            applied=1,
            reverted=0,
            discarded=0,
            has_outcome=True,
            match_summary="n/a",
        ),
        None,
        backup_created_at=datetime.now(timezone.utc),
    )
    assert row["revertable"] is False
    assert row["revert_tooltip"].startswith("entity backup creation ")


def test_revert_entity_confirmation_says_backup_time_and_all_languages() -> None:
    title, message = revert_entity_confirmation("s1", "entity backup creation 2026-02-03 10:05")
    assert title == "Revert entity"
    assert "2026-02-03 10:05" in message
    assert "ALL languages" in message
