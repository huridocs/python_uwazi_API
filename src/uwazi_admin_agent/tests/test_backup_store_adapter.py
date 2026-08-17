from datetime import datetime, timezone

import pytest

from uwazi_admin_agent.adapters.backup_store_adapter import FilesystemBackupStore
from uwazi_admin_agent.domain.manifest import MigrationManifest, RunStatus
from uwazi_admin_agent.domain.snapshot import EntitySnapshot


def _snapshot(shared_id: str, raw: dict) -> EntitySnapshot:
    return EntitySnapshot(
        shared_id=shared_id,
        raw=raw,
        captured_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def _manifest(run_id: str) -> MigrationManifest:
    return MigrationManifest(
        run_id=run_id,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        prompt="d",
        script="x = 1",
    )


def test_snapshot_round_trips(tmp_path) -> None:
    store = FilesystemBackupStore(tmp_path)
    snap = _snapshot("abc1", {"_id": "a1", "sharedId": "abc1", "title": "old", "relations": []})

    store.save_snapshot("run-1", snap)

    assert store.load_snapshot("run-1", "abc1") == snap


def test_load_snapshot_raises_when_absent(tmp_path) -> None:
    store = FilesystemBackupStore(tmp_path)
    with pytest.raises(FileNotFoundError):
        store.load_snapshot("run-1", "missing")


def test_manifest_round_trips(tmp_path) -> None:
    store = FilesystemBackupStore(tmp_path)
    manifest = _manifest("run-1")

    store.save_manifest("run-1", manifest)

    assert store.load_manifest("run-1") == manifest


def test_load_manifest_raises_when_absent(tmp_path) -> None:
    store = FilesystemBackupStore(tmp_path)
    with pytest.raises(FileNotFoundError):
        store.load_manifest("missing")


def test_update_status_persists(tmp_path) -> None:
    store = FilesystemBackupStore(tmp_path)
    store.save_manifest("run-1", _manifest("run-1"))

    store.update_status("run-1", RunStatus.EXECUTED)

    assert store.load_manifest("run-1").status == RunStatus.EXECUTED


def test_list_runs_lists_manifest_runs_sorted_and_ignores_bare_dirs(tmp_path) -> None:
    store = FilesystemBackupStore(tmp_path)
    store.save_manifest("run-2", _manifest("run-2"))
    store.save_manifest("run-1", _manifest("run-1"))
    (tmp_path / "junk").mkdir()  # a dir without a manifest is not a run

    assert store.list_runs() == ["run-1", "run-2"]


def test_list_runs_empty_when_root_missing(tmp_path) -> None:
    store = FilesystemBackupStore(tmp_path / "does-not-exist")
    assert store.list_runs() == []


def test_save_snapshot_safe_filename_for_unsafe_shared_id(tmp_path) -> None:
    store = FilesystemBackupStore(tmp_path)
    snap = _snapshot("weird/id", {"_id": "x", "sharedId": "weird/id"})

    store.save_snapshot("run-1", snap)

    assert store.load_snapshot("run-1", "weird/id") == snap
