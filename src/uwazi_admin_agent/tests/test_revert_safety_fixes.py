"""Regression tests for the revert-safety defects found in the production audit.

Each test pins one behaviour that could silently lose or duplicate data:

1. an entity updated and then deleted by the SAME run must be re-created by
   revert (it used to stay in ``manifest.modified``, so revert issued an update
   against a dead row and the 404 was swallowed);
2. a refused entity delete must NOT be recorded as deleted (revert would
   re-create a live entity);
3. a modified entity whose rows are all gone must FAIL the revert loudly (a
   silent skip reported success and left the run unretryable);
4. publish/permission grants and non-property connection rows are captured in
   the snapshot and restored — both are invisible to the entity write;
5. verification reports a publish state that did not come back;
6. a run rename re-keys the suggestion-store rows.

Isolated per AGENTS.md: real domain objects, in-memory ports as plain classes,
a real ``SqliteSuggestionStore`` over a tmp file. No mocks, no network.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, override

import pytest

from uwazi_admin_agent.adapters.sqlite_suggestion_store import SqliteSuggestionStore
from uwazi_admin_agent.domain.backup_decision import decide_backup, populate_manifest
from uwazi_admin_agent.domain.manifest import EntityIdentity, MigrationManifest, RunStatus
from uwazi_admin_agent.domain.publish_state import PublishState
from uwazi_admin_agent.domain.revert import build_revert_actions
from uwazi_admin_agent.domain.revert_gate import RevertRefusedError
from uwazi_admin_agent.domain.revert_verification import verify_revert
from uwazi_admin_agent.domain.snapshot import EntityRowSnapshot, EntitySnapshot
from uwazi_admin_agent.domain.suggestion import Suggestion, SuggestionFilter
from uwazi_admin_agent.ports.backup_store_port import BackupStorePort
from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort
from uwazi_admin_agent.ports.publish_state_port import PublishStatePort
from uwazi_admin_agent.ports.relationship_hub_port import RelationshipHubPort
from uwazi_admin_agent.use_cases.revert_run_use_case import RevertRunUseCase

pytestmark = pytest.mark.anyio
CAPTURED = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)


def _row(shared_id: str, **extra: Any) -> dict[str, Any]:
    return {"_id": f"{shared_id}_id", "sharedId": shared_id, "language": "en", "title": shared_id, "metadata": {}, **extra}


def _snapshot(shared_id: str, **extra: Any) -> EntitySnapshot:
    raw = _row(shared_id)
    return EntitySnapshot(
        shared_id=shared_id,
        internal_id=raw["_id"],
        language="en",
        raw=raw,
        captured_at=CAPTURED,
        rows=[EntityRowSnapshot(language="en", internal_id=raw["_id"], raw=raw)],
        **extra,
    )


def _manifest(**fields: Any) -> MigrationManifest:
    return MigrationManifest(run_id="run1", created_at=CAPTURED, prompt="p", script="s", status=RunStatus.EXECUTED, **fields)


class _Repo(EntityRepositoryPort):
    """Live-instance stand-in: only ``live`` sharedIds exist; every call is recorded."""

    def __init__(self, live: set[str] | None = None) -> None:
        self.live: set[str] = set(live or set())
        self.saved: list[dict[str, Any]] = []
        self.created: list[dict[str, Any]] = []

    @override
    async def get_raw_by_shared_id(self, shared_id: str, language: str | None = None) -> dict[str, Any]:
        if shared_id not in self.live:
            raise RuntimeError(f"Entity not found for sharedId={shared_id} language={language!r}")
        return _row(shared_id, language=language or "en")

    @override
    async def get_raw_by_internal_id(self, internal_id: str) -> dict[str, Any]:
        for shared_id in self.live:
            if shared_id == internal_id:
                return _row(shared_id)
        raise RuntimeError(f"Entity not found by _id={internal_id}")

    @override
    async def save_raw(self, raw: dict[str, Any]) -> None:
        if raw.get("sharedId") not in self.live:
            raise RuntimeError(f"save_raw failed (404): no row for {raw.get('sharedId')}")
        self.saved.append(raw)

    @override
    async def create_raw(self, raw: dict[str, Any]) -> str:
        self.created.append(raw)
        new_id = f"new-{len(self.created)}"
        self.live.add(new_id)
        return new_id

    @override
    async def delete_by_shared_id(self, shared_id: str) -> None:
        self.live.discard(shared_id)


class _Store(BackupStorePort):
    """Plain-dict manifest + snapshot store."""

    def __init__(self, manifest: MigrationManifest, snapshots: dict[str, EntitySnapshot] | None = None) -> None:
        self.manifest = manifest
        self.snapshots: dict[str, EntitySnapshot] = dict(snapshots or {})

    @override
    def save_snapshot(self, run_id: str, snapshot: EntitySnapshot) -> None:
        self.snapshots[snapshot.shared_id] = snapshot

    @override
    def load_snapshot(self, run_id: str, shared_id: str) -> EntitySnapshot:
        if shared_id not in self.snapshots:
            raise FileNotFoundError(f"No snapshot for {shared_id}")
        return self.snapshots[shared_id]

    @override
    def save_file_bytes(self, run_id: str, shared_id: str, file_id: str, data: bytes) -> None:
        raise NotImplementedError("not used by these tests")

    @override
    def load_file_bytes(self, run_id: str, shared_id: str, file_id: str) -> bytes:
        raise FileNotFoundError(file_id)

    @override
    def save_manifest(self, run_id: str, manifest: MigrationManifest) -> None:
        self.manifest = manifest

    @override
    def load_manifest(self, run_id: str) -> MigrationManifest:
        return self.manifest

    @override
    def update_status(self, run_id: str, status: RunStatus) -> None:
        self.manifest.status = status

    @override
    def list_runs(self) -> list[str]:
        return [self.manifest.run_id]

    @override
    def clear_run(self, run_id: str) -> None:
        self.snapshots.clear()

    @override
    def delete_run(self, run_id: str) -> None:
        self.snapshots.clear()

    @override
    def rename_run(self, old_id: str, new_id: str) -> None:
        self.manifest.run_id = new_id


class _Publish(PublishStatePort):
    """Grant store: records restores, serves whatever it currently holds."""

    def __init__(self, state: PublishState) -> None:
        self.state = state
        self.restored: list[tuple[str, PublishState]] = []

    @override
    async def read(self, shared_id: str, language: str | None = None) -> PublishState:
        return self.state

    @override
    async def restore(self, shared_id: str, state: PublishState) -> None:
        self.restored.append((shared_id, state))
        self.state = state


class _Hubs(RelationshipHubPort):
    """Connection-row store: records deletes and replays."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = list(rows or [])
        self.deleted: list[str] = []
        self.restored: list[dict[str, Any]] = []

    @override
    async def list_for_entity(self, shared_id: str, language: str | None = None) -> list[dict[str, Any]]:
        return list(self.rows)

    @override
    async def delete_rows(self, row_ids: list[str], language: str | None = None) -> None:
        self.deleted.extend(row_ids)

    @override
    async def restore_rows(self, rows: list[dict[str, Any]], language: str | None = None) -> None:
        self.restored.extend(rows)


# --- 1. update-then-delete ---------------------------------------------------


def test_delete_of_an_already_backed_up_entity_promotes_it_to_deleted() -> None:
    manifest = _manifest()
    snapshot = _snapshot("X")
    populate_manifest(manifest, decide_backup("update", ["X"], created=set(), backed_up=set()), {"X": snapshot})
    populate_manifest(manifest, decide_backup("delete", ["X"], created=set(), backed_up={"X"}), {"X": snapshot})

    assert [e.shared_id for e in manifest.modified] == []
    assert [e.shared_id for e in manifest.deleted] == ["X"]


async def test_update_then_delete_revert_recreates_instead_of_updating() -> None:
    manifest = _manifest()
    snapshot = _snapshot("X")
    populate_manifest(manifest, decide_backup("update", ["X"], created=set(), backed_up=set()), {"X": snapshot})
    populate_manifest(manifest, decide_backup("delete", ["X"], created=set(), backed_up={"X"}), {"X": snapshot})

    actions = build_revert_actions(manifest, lambda sid: snapshot)
    assert [type(a).__name__ for a in actions] == ["RecreateEntityAction"]

    repo = _Repo(live=set())
    await RevertRunUseCase(repo, _Store(manifest, {"X": snapshot})).revert("run1")

    assert repo.created and repo.saved == []
    assert manifest.status is RunStatus.REVERTED


# --- 2. refused delete -------------------------------------------------------


def test_refused_delete_is_not_promoted_to_deleted() -> None:
    from uwazi_admin_agent.use_cases.backup_intercept import BackupIntercept

    class _AnyRepo(_Repo):
        @override
        async def get_raw_by_shared_id(self, shared_id: str, language: str | None = None) -> dict[str, Any]:
            return _row(shared_id, language=language or "en")

    manifest = _manifest()
    loop = __import__("asyncio").new_event_loop()
    try:
        intercept = BackupIntercept(_AnyRepo(), _Store(manifest), manifest, "run1", "en", loop, settings_languages=["en"])

        def crud(payload: Any, language: str | None = None) -> Any:
            return payload

        def delete(shared_ids: list[str]) -> list[dict[str, Any]]:
            return [
                {"shared_id": "A", "success": False, "error": "Permission denied"},
                {"shared_id": "B", "success": True},
            ]

        decorated = intercept.decorate((crud, crud, delete, crud, crud, crud, crud))
        decorated["delete_entities"](["A", "B"])
    finally:
        loop.close()

    # The snapshot decision ran before the delete (the raw is only readable until
    # then), so both ids were recorded; the refusal must remove A again.
    assert [e.shared_id for e in manifest.deleted] == ["B"]


# --- 3. missing entity is a hard failure -------------------------------------


async def test_restore_of_a_vanished_entity_fails_loudly() -> None:
    manifest = _manifest(modified=[EntityIdentity(shared_id="GONE")])
    store = _Store(manifest, {"GONE": _snapshot("GONE")})
    repo = _Repo(live=set())

    with pytest.raises(RuntimeError, match="cannot restore"):
        await RevertRunUseCase(repo, store).revert("run1")

    assert manifest.status is RunStatus.FAILED


async def test_per_entity_revert_refuses_a_deleted_entity() -> None:
    manifest = _manifest(deleted=[EntityIdentity(shared_id="D", restored_shared_id="D_new")])
    store = _Store(manifest, {"D": _snapshot("D")})
    repo = _Repo(live={"D_new"})

    with pytest.raises(RevertRefusedError, match="deleted by the run"):
        await RevertRunUseCase(repo, store).revert_entity("run1", "D")

    assert repo.saved == []


# --- 4. publish state + connection hubs --------------------------------------


async def test_revert_restores_publish_state_and_citations() -> None:
    publish_before = PublishState(published=True, permissions=[{"refId": "public", "type": "public", "level": "read"}])
    citation_rows = [
        {"entity": "X", "template": None, "reference": {"text": "cited"}},
        {"entity": "Y", "template": "rtype"},
    ]
    snapshot = _snapshot("X", publish_state=publish_before, connection_hubs=citation_rows)
    manifest = _manifest(modified=[EntityIdentity(shared_id="X")])
    publish = _Publish(PublishState(published=False, permissions=[]))
    hubs = _Hubs([{"_id": "row-run-added", "entity": "X", "template": None, "reference": {"text": "added"}}])

    await RevertRunUseCase(
        _Repo(live={"X"}), _Store(manifest, {"X": snapshot}), publish_state=publish, relationship_hubs=hubs
    ).revert("run1")

    assert publish.restored == [("X", publish_before)]
    assert hubs.deleted == ["row-run-added"]
    assert len(hubs.restored) == len(citation_rows)


async def test_per_entity_revert_also_restores_publish_state_and_citations() -> None:
    publish_before = PublishState(published=True, permissions=[{"refId": "public", "type": "public", "level": "read"}])
    snapshot = _snapshot(
        "X",
        publish_state=publish_before,
        connection_hubs=[{"entity": "X", "template": None, "file": "f1"}, {"entity": "Y", "template": "rtype"}],
    )
    manifest = _manifest(modified=[EntityIdentity(shared_id="X")])
    publish = _Publish(PublishState(published=False, permissions=[]))
    hubs = _Hubs([])

    await RevertRunUseCase(
        _Repo(live={"X"}), _Store(manifest, {"X": snapshot}), publish_state=publish, relationship_hubs=hubs
    ).revert_entity("run1", "X")

    assert publish.restored == [("X", publish_before)]
    assert len(hubs.restored) == 2


def test_snapshot_property_hubs_are_not_captured() -> None:
    from uwazi_admin_agent.domain.connection_hubs import extract_connection_hubs

    reproducible = [
        {"hub": "h1", "entity": "A", "template": None},
        {"hub": "h1", "entity": "B", "template": "rtype"},
    ]
    citation = [
        {"hub": "h2", "entity": "A", "template": None, "reference": {"text": "q"}},
        {"hub": "h2", "entity": "B", "template": "rtype"},
    ]

    assert extract_connection_hubs(reproducible) == []
    assert len(extract_connection_hubs(citation)) == 2


# --- 5. verification sees publish drift --------------------------------------


def test_verification_flags_an_unrestored_publish_state() -> None:
    snapshot = _snapshot(
        "X", publish_state=PublishState(published=True, permissions=[{"refId": "public", "type": "public", "level": "read"}])
    )
    manifest = _manifest(modified=[EntityIdentity(shared_id="X")])

    result = verify_revert(
        manifest,
        {"X": snapshot},
        {"X": _row("X")},
        current_publish_states={"X": PublishState(published=False, permissions=[])},
    )

    assert result.ok is False
    assert [m.kind for m in result.mismatches] == ["publish"]


def test_verification_passes_when_publish_state_matches() -> None:
    state = PublishState(published=True, permissions=[{"refId": "public", "type": "public", "level": "read"}])
    manifest = _manifest(modified=[EntityIdentity(shared_id="X")])

    result = verify_revert(
        manifest, {"X": _snapshot("X", publish_state=state)}, {"X": _row("X")}, current_publish_states={"X": state}
    )

    assert result.ok is True


# --- 6. run rename -----------------------------------------------------------


def _suggestion(run_id: str, shared_id: str = "s1") -> Suggestion:
    return Suggestion(
        run_id=run_id,
        attempt=1,
        shared_id=shared_id,
        op="update",
        language="en",
        property_name="excerpt",
        proposed_value="x",
    )


def test_rename_run_rekeys_suggestion_rows(tmp_path: Path) -> None:
    store = SqliteSuggestionStore(tmp_path / "suggestions.db")
    try:
        store.add_suggestions([_suggestion("old")])
        store.rename_run("old", "renamed")

        rows, total = store.entity_rows("renamed", 1, SuggestionFilter(limit=10))
        old_rows, old_total = store.entity_rows("old", 1, SuggestionFilter(limit=10))
    finally:
        store.close()

    assert total == 1 and rows[0].shared_id == "s1"
    assert old_total == 0 and old_rows == []
