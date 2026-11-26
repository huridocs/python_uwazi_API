"""Regression tests for write-helper ``language`` kwarg tolerance (live-test fix).

The generated merge script called ``delete_entities([...], language="en")``,
but the bound ``delete_entities`` (and the publish helpers) did not accept a
``language`` argument → ``TypeError`` during execute (and the same error would
have failed the dummy gate, so the script that reached execute was a best-effort
emit). Delete/publish act on ALL language rows by ``sharedId`` (language is
semantically irrelevant), so the helpers now accept an IGNORED ``language`` kwarg
for signature consistency with the other helpers (the generator naturally passes
it). These tests pin that tolerance for the backup-intercepted wrappers, with
real in-memory ports (no mocks, no network).
"""

import asyncio
from datetime import datetime, timezone
from typing import Any, override

from uwazi_admin_agent.domain.manifest import MigrationManifest, RunStatus
from uwazi_admin_agent.domain.snapshot import EntitySnapshot
from uwazi_admin_agent.ports.backup_store_port import BackupStorePort
from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort
from uwazi_admin_agent.use_cases.backup_intercept import BackupIntercept

# --- intercepted wrappers (real mode) ---------------------------------------


def _fake_crud(recorder: list[tuple[str, tuple, dict]]) -> tuple:
    """Seven real callables standing in for the reused CRUD helpers (no ports)."""

    def _rec(name: str) -> Any:
        def fn(*args: Any, **kwargs: Any) -> Any:
            recorder.append((name, args, kwargs))
            return [{"success": True, "shared_id": "D1"}]

        return fn

    return tuple(
        _rec(n) for n in ("create", "update", "delete", "publish", "unpublish", "set_publish_status", "create_relationships")
    )


class _MemBackupStore(BackupStorePort):
    def __init__(self) -> None:
        self.snapshots: list[EntitySnapshot] = []

    @override
    def save_snapshot(self, run_id: str, snapshot: EntitySnapshot) -> None:
        self.snapshots.append(snapshot)

    @override
    def load_snapshot(self, run_id: str, shared_id: str) -> EntitySnapshot:
        raise FileNotFoundError

    @override
    def save_file_bytes(self, run_id: str, shared_id: str, file_id: str, data: bytes) -> None: ...

    @override
    def load_file_bytes(self, run_id: str, shared_id: str, file_id: str) -> bytes:
        raise FileNotFoundError

    @override
    def save_manifest(self, run_id: str, manifest: MigrationManifest) -> None: ...

    @override
    def load_manifest(self, run_id: str) -> MigrationManifest:
        raise FileNotFoundError

    @override
    def update_status(self, run_id: str, status: RunStatus) -> None: ...

    @override
    def clear_run(self, run_id: str) -> None: ...

    @override
    def list_runs(self) -> list[str]:
        return []

    @override
    def delete_run(self, run_id: str) -> None: ...

    @override
    def rename_run(self, old_id: str, new_id: str) -> None: ...


class _MemEntityRepo(EntityRepositoryPort):
    @override
    async def get_raw_by_shared_id(self, shared_id: str, language: str | None = None) -> dict[str, Any]:
        return {"_id": "o-" + shared_id, "sharedId": shared_id, "title": "T", "language": "en"}

    @override
    async def get_raw_by_internal_id(self, internal_id: str) -> dict[str, Any]:
        raise NotImplementedError

    @override
    async def save_raw(self, raw: dict[str, Any]) -> None: ...

    @override
    async def create_raw(self, raw: dict[str, Any]) -> str:
        raise NotImplementedError

    @override
    async def delete_by_shared_id(self, shared_id: str) -> None: ...


def _manifest() -> MigrationManifest:
    return MigrationManifest(
        run_id="run-1",
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        prompt="merge",
        script="x",
        status=RunStatus.PLANNED,
    )


def _intercept(
    loop: asyncio.AbstractEventLoop, manifest: MigrationManifest, backup_store: BackupStorePort | None = None
) -> BackupIntercept:
    return BackupIntercept(
        entity_repository=_MemEntityRepo(),
        backup_store=backup_store or _MemBackupStore(),
        manifest=manifest,
        run_id="run-1",
        language="en",
        loop=loop,
    )


def test_intercept_delete_accepts_ignored_language_and_snapshots() -> None:
    loop = asyncio.new_event_loop()
    try:
        manifest = _manifest()
        store = _MemBackupStore()
        intercept = _intercept(loop, manifest, backup_store=store)
        crud = _fake_crud([])
        intercepted = intercept.decorate(crud)

        # The generator's natural call shape: delete_entities with a language kwarg.
        result = intercepted["delete_entities"](["E1"], language="es")

        assert result == [{"success": True, "shared_id": "D1"}]
        # The entity was snapshotted into manifest.deleted (the snapshot uses the run
        # language "en", NOT the ignored "es").
        assert [e.shared_id for e in manifest.deleted] == ["E1"]
        assert store.snapshots[0].shared_id == "E1"
    finally:
        loop.close()


def test_intercept_delete_without_language_still_snapshots() -> None:
    loop = asyncio.new_event_loop()
    try:
        manifest = _manifest()
        intercepted = _intercept(loop, manifest).decorate(_fake_crud([]))

        intercepted["delete_entities"](["E1"])

        assert [e.shared_id for e in manifest.deleted] == ["E1"]
    finally:
        loop.close()


def test_intercept_publish_helpers_accept_ignored_language() -> None:
    loop = asyncio.new_event_loop()
    try:
        manifest = _manifest()
        calls: list[tuple[str, tuple, dict]] = []
        intercepted = _intercept(loop, manifest).decorate(_fake_crud(calls))

        # No TypeError; the helpers tolerate the ignored language kwarg.
        intercepted["publish_entities"](["E1"], language="es")
        intercepted["unpublish_entities"](["E1"], language="es")
        intercepted["set_publish_status"](["E1"], False, language="es")

        # All three underlying calls ran (no language forwarded).
        assert [c[0] for c in calls] == ["publish", "unpublish", "set_publish_status"]
        assert all(c[2] == {} for c in calls)
        # First-touch: only the FIRST op on E1 snapshots it (the repeats are no-ops
        # for the touch set) - manifest.modified carries E1 once.
        assert [e.shared_id for e in manifest.modified] == ["E1"]
    finally:
        loop.close()
