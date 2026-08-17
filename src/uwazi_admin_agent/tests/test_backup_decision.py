from datetime import datetime, timezone
from typing import Any

from uwazi_admin_agent.domain.backup_decision import (
    BackupDecision,
    build_rewired_relationships,
    decide_backup,
    populate_manifest,
)
from uwazi_admin_agent.domain.manifest import MigrationManifest, RunStatus
from uwazi_admin_agent.domain.snapshot import EntitySnapshot


def _snapshot(shared_id: str, raw: dict[str, Any]) -> EntitySnapshot:
    return EntitySnapshot(
        shared_id=shared_id,
        internal_id=raw.get("_id"),
        language=raw.get("language"),
        raw=raw,
        captured_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def _manifest() -> MigrationManifest:
    return MigrationManifest(
        run_id="run-1",
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        prompt="d",
        script="x = 1",
        status=RunStatus.PLANNED,
    )


# --- decide_backup: create --------------------------------------------------


def test_decide_create_returns_empty() -> None:
    decision = decide_backup("create", [], created=set(), backed_up=set())
    assert decision.snapshot_ids == []
    assert decision.add_modified == []
    assert decision.add_deleted == []
    assert decision.add_created == []
    assert decision.remove_from_created == []


# --- decide_backup: update --------------------------------------------------


def test_decide_update_all_new() -> None:
    decision = decide_backup("update", ["A", "B"], created=set(), backed_up=set())
    assert decision.snapshot_ids == ["A", "B"]
    assert decision.add_modified == ["A", "B"]
    assert decision.add_deleted == []


def test_decide_update_skips_created() -> None:
    decision = decide_backup("update", ["A", "B"], created={"A"}, backed_up=set())
    assert decision.snapshot_ids == ["B"]
    assert decision.add_modified == ["B"]


def test_decide_update_skips_backed_up() -> None:
    decision = decide_backup("update", ["A", "B"], created=set(), backed_up={"A"})
    assert decision.snapshot_ids == ["B"]
    assert decision.add_modified == ["B"]


def test_decide_update_mixed() -> None:
    decision = decide_backup("update", ["A", "B", "C", "D"], created={"A"}, backed_up={"B"})
    assert decision.snapshot_ids == ["C", "D"]
    assert decision.add_modified == ["C", "D"]


def test_decide_update_empty_ids() -> None:
    decision = decide_backup("update", [], created=set(), backed_up=set())
    assert decision.snapshot_ids == []


# --- decide_backup: delete --------------------------------------------------


def test_decide_delete_all_new() -> None:
    decision = decide_backup("delete", ["A", "B"], created=set(), backed_up=set())
    assert decision.snapshot_ids == ["A", "B"]
    assert decision.add_deleted == ["A", "B"]
    assert decision.add_modified == []
    assert decision.remove_from_created == []


def test_decide_delete_created_then_delete() -> None:
    decision = decide_backup("delete", ["A", "B"], created={"A"}, backed_up=set())
    assert decision.snapshot_ids == ["B"]
    assert decision.add_deleted == ["B"]
    assert decision.remove_from_created == ["A"]


def test_decide_delete_skips_backed_up() -> None:
    decision = decide_backup("delete", ["A", "B"], created=set(), backed_up={"A"})
    assert decision.snapshot_ids == ["B"]
    assert decision.add_deleted == ["B"]


def test_decide_delete_mixed() -> None:
    decision = decide_backup("delete", ["A", "B", "C", "D"], created={"A"}, backed_up={"B"})
    assert decision.snapshot_ids == ["C", "D"]
    assert decision.add_deleted == ["C", "D"]
    assert decision.remove_from_created == ["A"]


# --- decide_backup: create_relationships ------------------------------------


def test_decide_rewire_same_as_update() -> None:
    decision = decide_backup("create_relationships", ["A", "B"], created=set(), backed_up=set())
    assert decision.snapshot_ids == ["A", "B"]
    assert decision.add_modified == ["A", "B"]


def test_decide_rewire_skips_created() -> None:
    decision = decide_backup("create_relationships", ["A", "B"], created={"A"}, backed_up=set())
    assert decision.snapshot_ids == ["B"]
    assert decision.add_modified == ["B"]


# --- populate_manifest: add_modified ----------------------------------------


def test_populate_add_modified_from_snapshots() -> None:
    manifest = _manifest()
    decision = BackupDecision(snapshot_ids=["A", "B"], add_modified=["A", "B"])
    snapshots = {
        "A": _snapshot("A", {"_id": "a1", "language": "en", "title": "old A"}),
        "B": _snapshot("B", {"_id": "b1", "language": "es", "title": "old B"}),
    }

    populate_manifest(manifest, decision, snapshots)

    assert [e.shared_id for e in manifest.modified] == ["A", "B"]
    assert manifest.modified[0].internal_id == "a1"
    assert manifest.modified[0].language == "en"
    assert manifest.modified[1].internal_id == "b1"
    assert manifest.modified[1].language == "es"


# --- populate_manifest: add_deleted -----------------------------------------


def test_populate_add_deleted_from_snapshots() -> None:
    manifest = _manifest()
    decision = BackupDecision(snapshot_ids=["X"], add_deleted=["X"])
    snapshots = {"X": _snapshot("X", {"_id": "x1", "language": "en"})}

    populate_manifest(manifest, decision, snapshots)

    assert [e.shared_id for e in manifest.deleted] == ["X"]
    assert manifest.deleted[0].internal_id == "x1"


# --- populate_manifest: add_created -----------------------------------------


def test_populate_add_created_minimal_identity() -> None:
    manifest = _manifest()
    decision = BackupDecision(add_created=["NEW1", "NEW2"])

    populate_manifest(manifest, decision, snapshots={})

    assert [e.shared_id for e in manifest.created] == ["NEW1", "NEW2"]
    assert all(e.internal_id is None for e in manifest.created)
    assert all(e.language is None for e in manifest.created)


# --- populate_manifest: remove_from_created ---------------------------------


def test_populate_remove_from_created() -> None:
    from uwazi_admin_agent.domain.snapshot import EntityIdentity

    manifest = _manifest()
    manifest.created = [EntityIdentity(shared_id=s) for s in ["A", "B", "C"]]
    decision = BackupDecision(remove_from_created=["B"])

    populate_manifest(manifest, decision, snapshots={})

    assert [e.shared_id for e in manifest.created] == ["A", "C"]


def test_populate_remove_from_created_multiple() -> None:
    from uwazi_admin_agent.domain.snapshot import EntityIdentity

    manifest = _manifest()
    manifest.created = [EntityIdentity(shared_id=s) for s in ["A", "B", "C", "D"]]
    decision = BackupDecision(remove_from_created=["A", "C"])

    populate_manifest(manifest, decision, snapshots={})

    assert [e.shared_id for e in manifest.created] == ["B", "D"]


# --- populate_manifest: combined --------------------------------------------


def test_populate_combined_decision() -> None:
    from uwazi_admin_agent.domain.snapshot import EntityIdentity

    manifest = _manifest()
    manifest.created = [EntityIdentity(shared_id="OLD")]
    decision = BackupDecision(
        snapshot_ids=["M", "D"],
        add_modified=["M"],
        add_deleted=["D"],
        add_created=["NEW"],
        remove_from_created=["OLD"],
    )
    snapshots = {
        "M": _snapshot("M", {"_id": "m1", "language": "en"}),
        "D": _snapshot("D", {"_id": "d1", "language": "en"}),
    }

    populate_manifest(manifest, decision, snapshots)

    assert [e.shared_id for e in manifest.modified] == ["M"]
    assert [e.shared_id for e in manifest.deleted] == ["D"]
    assert [e.shared_id for e in manifest.created] == ["NEW"]


def test_populate_empty_decision_no_change() -> None:
    manifest = _manifest()
    populate_manifest(manifest, BackupDecision(), snapshots={})
    assert manifest.modified == []
    assert manifest.deleted == []
    assert manifest.created == []
    assert manifest.rewired == []


# --- build_rewired_relationships -------------------------------------------


def test_build_rewired_extracts_relations() -> None:
    raws = {
        "A": {"_id": "a1", "relations": [{"_id": "r1", "label": "X"}]},
        "B": {"_id": "b1", "relations": []},
    }

    rewired = build_rewired_relationships(["A", "B"], raws, language="en")

    assert len(rewired) == 2
    assert rewired[0].entity.shared_id == "A"
    assert rewired[0].entity.language == "en"
    assert rewired[0].property_name == "relations"
    assert rewired[0].before == [{"_id": "r1", "label": "X"}]
    assert rewired[1].before == []


def test_build_rewired_skips_missing_raws() -> None:
    raws = {"A": {"relations": [1]}}
    rewired = build_rewired_relationships(["A", "B"], raws, language="en")
    assert len(rewired) == 1
    assert rewired[0].entity.shared_id == "A"


def test_build_rewired_defaults_to_empty_list() -> None:
    raws = {"A": {"_id": "a1"}}  # no "relations" key
    rewired = build_rewired_relationships(["A"], raws, language="es")
    assert len(rewired) == 1
    assert rewired[0].before == []


def test_build_rewired_empty_input() -> None:
    assert build_rewired_relationships([], {}, language="en") == []
