"""Isolated unit tests for the audit-record shape (Phase 6 DoD).

No mocks, no network — literal inputs + pydantic round-trip + plain assertions.
"""

from datetime import datetime, timezone

from uwazi_admin_agent.domain.audit_record import (
    AuditOutcome,
    AuditRecord,
    AuditStep,
    make_audit_record,
)

# --- AuditStep / AuditOutcome values ---------------------------------------


def test_audit_step_values() -> None:
    assert AuditStep.EXECUTE.value == "execute"
    assert AuditStep.REVERT.value == "revert"


def test_audit_outcome_values() -> None:
    assert AuditOutcome.SUCCESS.value == "success"
    assert AuditOutcome.FAILURE.value == "failure"


# --- AuditRecord construction + round-trip ---------------------------------


def test_audit_record_constructs_with_literals() -> None:
    ts = datetime(2026, 8, 18, 12, 0, 0, tzinfo=timezone.utc)

    record = AuditRecord(
        timestamp=ts,
        run_id="run-1",
        step=AuditStep.EXECUTE,
        op_kind="update",
        shared_ids=["A", "B"],
        outcome=AuditOutcome.SUCCESS,
        detail=None,
    )

    assert record.timestamp == ts
    assert record.run_id == "run-1"
    assert record.step == AuditStep.EXECUTE
    assert record.op_kind == "update"
    assert record.shared_ids == ["A", "B"]
    assert record.outcome == AuditOutcome.SUCCESS
    assert record.detail is None


def test_audit_record_default_optional_fields() -> None:
    ts = datetime(2026, 8, 18, 12, 0, 0, tzinfo=timezone.utc)

    record = AuditRecord(
        timestamp=ts,
        run_id="run-1",
        step=AuditStep.REVERT,
        op_kind="restore_entity",
        shared_ids=[],
        outcome=AuditOutcome.SUCCESS,
    )

    assert record.shared_ids == []
    assert record.detail is None


def test_audit_record_round_trips_through_json() -> None:
    ts = datetime(2026, 8, 18, 12, 0, 0, tzinfo=timezone.utc)
    record = AuditRecord(
        timestamp=ts,
        run_id="run-2",
        step=AuditStep.EXECUTE,
        op_kind="cap_exceeded",
        shared_ids=[],
        outcome=AuditOutcome.FAILURE,
        detail="touched 1001 exceeding 1000",
    )

    restored = AuditRecord.model_validate_json(record.model_dump_json())

    assert restored == record


def test_audit_record_is_frozen() -> None:
    record = AuditRecord(
        timestamp=datetime(2026, 8, 18, tzinfo=timezone.utc),
        run_id="r",
        step=AuditStep.EXECUTE,
        op_kind="update",
        shared_ids=["A"],
        outcome=AuditOutcome.SUCCESS,
    )

    import pytest

    with pytest.raises(Exception):
        record.op_kind = "delete"  # type: ignore[misc]


# --- make_audit_record factory stamps now ----------------------------------


def test_make_audit_record_stamps_current_utc_time() -> None:
    before = datetime.now(timezone.utc)

    record = make_audit_record(
        run_id="run-1",
        step=AuditStep.EXECUTE,
        op_kind="update",
        shared_ids=["A"],
        outcome=AuditOutcome.SUCCESS,
    )

    after = datetime.now(timezone.utc)
    assert before <= record.timestamp <= after
    assert record.timestamp.tzinfo is not None
    assert record.run_id == "run-1"
    assert record.op_kind == "update"
    assert record.shared_ids == ["A"]


def test_make_audit_record_copies_shared_ids() -> None:
    ids = ["A", "B"]
    record = make_audit_record("r", AuditStep.REVERT, "restore_entity", ids, AuditOutcome.SUCCESS)

    ids.append("C")  # mutating the source must not affect the record
    assert record.shared_ids == ["A", "B"]


def test_make_audit_record_with_detail() -> None:
    record = make_audit_record("r", AuditStep.EXECUTE, "cap_exceeded", [], AuditOutcome.FAILURE, detail="exceeded")
    assert record.detail == "exceeded"
    assert record.outcome == AuditOutcome.FAILURE


# --- op_kind is a free string (new ops don't require enum churn) -----------


def test_audit_record_op_kind_accepts_arbitrary_string() -> None:
    record = AuditRecord(
        timestamp=datetime(2026, 8, 18, tzinfo=timezone.utc),
        run_id="r",
        step=AuditStep.EXECUTE,
        op_kind="some_future_phase8_op",
        shared_ids=[],
        outcome=AuditOutcome.SUCCESS,
    )
    assert record.op_kind == "some_future_phase8_op"
