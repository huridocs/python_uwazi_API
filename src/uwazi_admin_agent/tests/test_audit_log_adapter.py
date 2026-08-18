"""Isolated unit tests for the JSONL audit-log adapter (Phase 6 DoD).

No mocks, no network — a tmp runs root + literal :class:`AuditRecord`s + plain
assertions (the adapter is real filesystem I/O over a tmp path, which is the
AGENTS.md-sanctioned "tiny real in-memory class" equivalent for filesystem
adapters — tmp dirs are deterministic and offline).
"""

from datetime import datetime, timezone
from pathlib import Path

from uwazi_admin_agent.adapters.audit_log_adapter import AUDIT_FILENAME, JsonlAuditLog
from uwazi_admin_agent.domain.audit_record import AuditOutcome, AuditRecord, AuditStep


def _record(op_kind: str, outcome: AuditOutcome = AuditOutcome.SUCCESS, detail: str | None = None) -> AuditRecord:
    return AuditRecord(
        timestamp=datetime(2026, 8, 18, 12, 0, 0, tzinfo=timezone.utc),
        run_id="run-1",
        step=AuditStep.EXECUTE,
        op_kind=op_kind,
        shared_ids=["A"] if outcome == AuditOutcome.SUCCESS else [],
        outcome=outcome,
        detail=detail,
    )


# --- append + load round-trip -----------------------------------------------


def test_append_writes_one_jsonl_line(tmp_path: Path) -> None:
    log = JsonlAuditLog(tmp_path)
    log.append("run-1", _record("update"))

    path = tmp_path / "run-1" / AUDIT_FILENAME
    assert path.is_file()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert '"op_kind":"update"' in lines[0]


def test_append_then_load_returns_records_in_order(tmp_path: Path) -> None:
    log = JsonlAuditLog(tmp_path)
    log.append("run-1", _record("update"))
    log.append("run-1", _record("delete"))
    log.append("run-1", _record("cap_exceeded", AuditOutcome.FAILURE, detail="exceeded"))

    records = log.load("run-1")

    assert [r.op_kind for r in records] == ["update", "delete", "cap_exceeded"]
    assert records[0].outcome == AuditOutcome.SUCCESS
    assert records[2].outcome == AuditOutcome.FAILURE
    assert records[2].detail == "exceeded"


def test_load_empty_run_returns_empty_list(tmp_path: Path) -> None:
    log = JsonlAuditLog(tmp_path)
    assert log.load("never-written") == []


def test_load_skips_blank_lines(tmp_path: Path) -> None:
    log = JsonlAuditLog(tmp_path)
    log.append("run-1", _record("update"))
    # Append a blank line by hand (defensive against editors / partial writes).
    (tmp_path / "run-1" / AUDIT_FILENAME).write_text(
        (tmp_path / "run-1" / AUDIT_FILENAME).read_text(encoding="utf-8") + "\n\n",
        encoding="utf-8",
    )

    records = log.load("run-1")
    assert len(records) == 1
    assert records[0].op_kind == "update"


# --- multiple runs are isolated --------------------------------------------


def test_runs_are_isolated_by_run_id(tmp_path: Path) -> None:
    log = JsonlAuditLog(tmp_path)
    log.append("alpha", _record("update"))
    log.append("beta", _record("delete"))

    assert [r.op_kind for r in log.load("alpha")] == ["update"]
    assert [r.op_kind for r in log.load("beta")] == ["delete"]


# --- root may not exist yet -------------------------------------------------


def test_append_creates_run_dir_under_nonexistent_root(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    log = JsonlAuditLog(root)
    log.append("run-1", _record("update"))

    assert (root / "run-1" / AUDIT_FILENAME).is_file()


# --- full round-trip preserves every field ---------------------------------


def test_round_trip_preserves_all_fields(tmp_path: Path) -> None:
    log = JsonlAuditLog(tmp_path)
    original = _record("update", AuditOutcome.SUCCESS, detail="ok")
    log.append("run-1", original)

    [restored] = log.load("run-1")

    assert restored == original
    assert restored.timestamp == original.timestamp
    assert restored.shared_ids == ["A"]
    assert restored.detail == "ok"
