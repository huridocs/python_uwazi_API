"""Isolated unit tests for the runs-table execution-history derivation.

No real Uwazi instance, no mocks: pure filtering of literal :class:`AuditRecord`
objects, plus a real on-disk ``JsonlAuditLog`` rooted at a temp dir (offline).
"""

from datetime import datetime, timezone

from uwazi_admin_agent.adapters.audit_log_adapter import JsonlAuditLog
from uwazi_admin_agent.domain.audit_record import (
    AuditOutcome,
    AuditRecord,
    AuditStep,
)
from uwazi_admin_agent.drivers.web.run_service import (
    ExecutionEvent,
    _run_level_events,
    get_execution_history,
)


def _record(
    op_kind: str,
    step: AuditStep,
    shared_ids: list[str],
    outcome: AuditOutcome = AuditOutcome.SUCCESS,
    detail: str | None = None,
    ts: datetime | None = None,
) -> AuditRecord:
    return AuditRecord(
        timestamp=ts or datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        run_id="r1",
        step=step,
        op_kind=op_kind,
        shared_ids=list(shared_ids),
        outcome=outcome,
        detail=detail,
    )


def test_run_level_events_keeps_execute_and_revert_only() -> None:
    records = [
        _record("execute", AuditStep.EXECUTE, [], AuditOutcome.SUCCESS),
        _record("update", AuditStep.EXECUTE, ["A"]),
        _record("revert", AuditStep.REVERT, [], AuditOutcome.SUCCESS),
        # run-level record (empty shared_ids) but a non-execution op_kind -> excluded
        _record("cap_exceeded", AuditStep.EXECUTE, [], AuditOutcome.FAILURE, detail="x"),
        _record("restore_entity", AuditStep.REVERT, ["A"]),
    ]
    kept = _run_level_events(records)
    assert [r.op_kind for r in kept] == ["execute", "revert"]


def test_run_level_events_empty() -> None:
    assert _run_level_events([]) == []


def test_get_execution_history_newest_first_and_maps_fields(tmp_path) -> None:
    log = JsonlAuditLog(tmp_path)
    t_old = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    t_new = datetime(2026, 1, 2, 10, 0, 0, tzinfo=timezone.utc)
    log.append(
        "r1",
        AuditRecord(
            timestamp=t_old,
            run_id="r1",
            step=AuditStep.REVERT,
            op_kind="revert",
            shared_ids=[],
            outcome=AuditOutcome.SUCCESS,
        ),
    )
    log.append(
        "r1",
        AuditRecord(
            timestamp=t_new,
            run_id="r1",
            step=AuditStep.EXECUTE,
            op_kind="update",
            shared_ids=["A"],
            outcome=AuditOutcome.SUCCESS,
        ),
    )
    log.append(
        "r1",
        AuditRecord(
            timestamp=t_new,
            run_id="r1",
            step=AuditStep.EXECUTE,
            op_kind="execute",
            shared_ids=[],
            outcome=AuditOutcome.FAILURE,
            detail="boom",
        ),
    )

    events = get_execution_history("r1", audit_log=log)

    # newest first: the failed execute (t_new) precedes the revert (t_old)
    assert [(e.type, e.outcome) for e in events] == [("execute", "failure"), ("revert", "success")]
    assert events[0].detail == "boom"
    assert events[1].detail is None
    assert isinstance(events[0], ExecutionEvent)


def test_get_execution_history_missing_run_empty(tmp_path) -> None:
    log = JsonlAuditLog(tmp_path)
    assert get_execution_history("never-written", audit_log=log) == []
