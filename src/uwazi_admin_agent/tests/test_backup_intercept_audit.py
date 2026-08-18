"""Isolated regression tests for :class:`BackupIntercept`'s audit-emit wiring (Phase 6).

These exist because a real ``execute`` run surfaced a call-signature bug
(``AuditLogPort.append(run_id, record)`` called with only the record) that the
adapter-only tests in ``test_audit_log_adapter.py`` could not catch — the
adapter tests call ``append`` directly with the correct 2-arg form, while the
production callers go through ``BackupIntercept._emit`` / ``_enforce_cap``,
which were not exercised by any unit test. The fix is verified here by
constructing a :class:`BackupIntercept` with tiny real in-memory ports (no
mocks, no network — the AGENTS.md-sanctioned pattern) and driving the emit
paths directly.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, override

import pytest

from uwazi_admin_agent.domain.audit_record import AuditOutcome, AuditStep
from uwazi_admin_agent.domain.cap_enforcement import CapExceededError
from uwazi_admin_agent.domain.manifest import MigrationManifest, RunStatus
from uwazi_admin_agent.domain.snapshot import EntityIdentity, EntitySnapshot
from uwazi_admin_agent.ports.audit_log_port import AuditLogPort
from uwazi_admin_agent.ports.backup_store_port import BackupStorePort
from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort
from uwazi_admin_agent.use_cases.backup_intercept import BackupIntercept

# --- in-memory ports (real classes, not mocks) ------------------------------


class InMemoryAuditLog(AuditLogPort):
    def __init__(self) -> None:
        self.records: list[tuple[str, Any]] = []  # (run_id, record) pairs

    @override
    def append(self, run_id: str, record: Any) -> None:
        self.records.append((run_id, record))

    @override
    def load(self, run_id: str) -> list[Any]:
        return [r for rid, r in self.records if rid == run_id]


class InMemoryBackupStore(BackupStorePort):
    @override
    def save_snapshot(self, run_id: str, snapshot: EntitySnapshot) -> None: ...

    @override
    def load_snapshot(self, run_id: str, shared_id: str) -> EntitySnapshot: ...

    @override
    def save_manifest(self, run_id: str, manifest: MigrationManifest) -> None: ...

    @override
    def load_manifest(self, run_id: str) -> MigrationManifest: ...

    @override
    def update_status(self, run_id: str, status: RunStatus) -> None: ...

    @override
    def list_runs(self) -> list[str]: ...


class InMemoryEntityRepository(EntityRepositoryPort):
    @override
    async def get_raw_by_shared_id(self, shared_id: str, language: str | None = None) -> dict[str, Any]:
        return {}

    @override
    async def get_raw_by_internal_id(self, internal_id: str) -> dict[str, Any]:
        return {}

    @override
    async def save_raw(self, raw: dict[str, Any]) -> None: ...

    @override
    async def delete_by_shared_id(self, shared_id: str) -> None: ...


# --- helpers ----------------------------------------------------------------


def _manifest() -> MigrationManifest:
    return MigrationManifest(
        run_id="run-1",
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        prompt="d",
        script="x = 1",
        status=RunStatus.PLANNED,
    )


def _intercept(audit_log: AuditLogPort | None, cap: int = 1000) -> BackupIntercept:
    return BackupIntercept(
        entity_repository=InMemoryEntityRepository(),
        backup_store=InMemoryBackupStore(),
        manifest=_manifest(),
        run_id="run-1",
        language="en",
        loop=None,
        audit_log=audit_log,
        cap=cap,
    )


# --- _emit: appends a record with the correct (run_id, record) call ---------


def test_emit_appends_record_to_audit_log() -> None:
    log = InMemoryAuditLog()
    intercept = _intercept(log)

    intercept._emit("update", ["A", "B"])

    assert len(log.records) == 1
    run_id, record = log.records[0]
    assert run_id == "run-1"
    assert record.run_id == "run-1"
    assert record.step == AuditStep.EXECUTE
    assert record.op_kind == "update"
    assert record.shared_ids == ["A", "B"]
    assert record.outcome == AuditOutcome.SUCCESS


def test_emit_with_failure_outcome_and_detail() -> None:
    log = InMemoryAuditLog()
    intercept = _intercept(log)

    intercept._emit("cap_exceeded", [], outcome=AuditOutcome.FAILURE, detail="exceeded")

    _, record = log.records[0]
    assert record.outcome == AuditOutcome.FAILURE
    assert record.op_kind == "cap_exceeded"
    assert record.detail == "exceeded"


def test_emit_is_noop_when_no_audit_log() -> None:
    intercept = _intercept(audit_log=None)

    # Must not raise — the no-audit-log path is the production default for
    # existing tests and for any caller that hasn't wired an audit log.
    intercept._emit("update", ["A"])


# --- _enforce_cap: emits a cap_exceeded record before raising ----------------


def test_enforce_cap_emits_failure_record_before_raising() -> None:
    log = InMemoryAuditLog()
    intercept = _intercept(log, cap=2)
    # Grow the touch set past the cap.
    for sid in ("A", "B", "C"):
        intercept._manifest.modified.append(EntityIdentity(shared_id=sid))

    with pytest.raises(CapExceededError):
        intercept._enforce_cap()

    assert len(log.records) == 1
    _, record = log.records[0]
    assert record.op_kind == "cap_exceeded"
    assert record.outcome == AuditOutcome.FAILURE
    assert record.detail is not None
    assert "3" in record.detail  # the touch count


def test_enforce_cap_under_cap_emits_nothing() -> None:
    log = InMemoryAuditLog()
    intercept = _intercept(log, cap=10)

    intercept._enforce_cap()  # empty manifest, cap 10 — no raise, no record

    assert log.records == []


# --- _record_created: populates manifest + enforces cap ---------------------


def test_record_created_populates_manifest_and_enforces_cap() -> None:
    log = InMemoryAuditLog()
    intercept = _intercept(log, cap=1)

    intercept._record_created([{"shared_id": "NEW1", "success": True}])

    assert [e.shared_id for e in intercept._manifest.created] == ["NEW1"]

    # A second created entity exceeds the cap of 1 — must raise.
    with pytest.raises(CapExceededError):
        intercept._record_created([{"shared_id": "NEW2", "success": True}])
    # The cap_exceeded record was emitted before the raise.
    assert any(r.op_kind == "cap_exceeded" for _, r in log.records)


def test_record_created_skips_unsuccessful_results() -> None:
    log = InMemoryAuditLog()
    intercept = _intercept(log)

    intercept._record_created([{"shared_id": "X", "success": False}])

    assert intercept._manifest.created == []
    assert log.records == []  # nothing snapshotted, nothing audited
