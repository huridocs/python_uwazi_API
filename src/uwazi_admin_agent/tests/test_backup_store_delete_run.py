from datetime import datetime, timezone

from uwazi_admin_agent.adapters.backup_store_adapter import FilesystemBackupStore
from uwazi_admin_agent.domain.manifest import MigrationManifest
from uwazi_admin_agent.domain.snapshot import EntitySnapshot


def _manifest(run_id: str) -> MigrationManifest:
    return MigrationManifest(
        run_id=run_id,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        prompt="d",
        script="x = 1",
    )


def _snapshot(shared_id: str) -> EntitySnapshot:
    return EntitySnapshot(
        shared_id=shared_id,
        raw={"_id": "a1", "sharedId": shared_id, "title": "old", "relations": []},
        captured_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def test_delete_run_removes_entire_folder(tmp_path) -> None:
    store = FilesystemBackupStore(tmp_path)
    store.save_manifest("run-1", _manifest("run-1"))
    store.save_snapshot("run-1", _snapshot("abc1"))
    store.save_file_bytes("run-1", "abc1", "f1", b"BYTES")

    store.delete_run("run-1")

    assert not (tmp_path / "run-1").exists()
    assert store.list_runs() == []


def test_delete_run_noop_when_absent(tmp_path) -> None:
    store = FilesystemBackupStore(tmp_path)

    store.delete_run("never-existed")  # must not raise

    assert store.list_runs() == []


def test_delete_run_preserves_other_runs(tmp_path) -> None:
    store = FilesystemBackupStore(tmp_path)
    store.save_manifest("run-1", _manifest("run-1"))
    store.save_manifest("run-2", _manifest("run-2"))

    store.delete_run("run-1")

    assert store.list_runs() == ["run-2"]
    assert store.load_manifest("run-2").run_id == "run-2"
