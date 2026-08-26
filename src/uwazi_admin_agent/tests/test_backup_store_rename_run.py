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


def test_rename_run_moves_folder_and_rewrites_manifest_run_id(tmp_path) -> None:
    store = FilesystemBackupStore(tmp_path)
    store.save_manifest("run-1", _manifest("run-1"))
    store.save_snapshot("run-1", _snapshot("abc1"))
    store.save_file_bytes("run-1", "abc1", "f1", b"BYTES")

    store.rename_run("run-1", "run-2")

    assert not (tmp_path / "run-1").exists()
    assert (tmp_path / "run-2" / "manifest.json").is_file()
    # Snapshots + captured file bytes travel with the folder.
    assert store.load_snapshot("run-2", "abc1") == _snapshot("abc1")
    assert store.load_file_bytes("run-2", "abc1", "f1") == b"BYTES"
    # The manifest's identity field is rewritten to the new id.
    assert store.load_manifest("run-2").run_id == "run-2"
    assert store.list_runs() == ["run-2"]


def test_rename_run_renames_inner_prompt_snapshot_copy(tmp_path) -> None:
    store = FilesystemBackupStore(tmp_path)
    store.save_manifest("run-1", _manifest("run-1"))
    # Mirror RunsConfigLoader.load_active_path: a copy of the prompt named <id>.yaml.
    (tmp_path / "run-1" / "run-1.yaml").write_text("prompt: p", encoding="utf-8")

    store.rename_run("run-1", "run-2")

    assert not (tmp_path / "run-2" / "run-1.yaml").exists()
    assert (tmp_path / "run-2" / "run-2.yaml").is_file()


def test_rename_run_raises_when_source_absent(tmp_path) -> None:
    store = FilesystemBackupStore(tmp_path)

    try:
        store.rename_run("never-existed", "run-2")
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("expected FileNotFoundError")

    assert store.list_runs() == []


def test_rename_run_raises_when_target_exists(tmp_path) -> None:
    store = FilesystemBackupStore(tmp_path)
    store.save_manifest("run-1", _manifest("run-1"))
    store.save_manifest("run-2", _manifest("run-2"))

    try:
        store.rename_run("run-1", "run-2")
    except FileExistsError:
        pass
    else:
        raise AssertionError("expected FileExistsError")

    # Both runs are untouched.
    assert store.list_runs() == ["run-1", "run-2"]
    assert store.load_manifest("run-1").run_id == "run-1"
    assert store.load_manifest("run-2").run_id == "run-2"
