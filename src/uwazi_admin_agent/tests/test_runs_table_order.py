"""Isolated test for the tasks table's default order (per AGENTS.md).

The operator asked for the task list sorted by creation date, newest first. The
order has two halves — the server's rows and the table's own sort — so the row
payload is pinned here: it carries a NUMERIC ``created_ts`` that the ``Created``
column sorts on, alongside the human ``created`` text it renders.

Real objects only: a ``RunSummary`` value and the row builder.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from uwazi_admin_agent.domain.manifest import RunStatus
from uwazi_admin_agent.drivers.web.app.broadcast import _creating_to_row, _summary_to_row
from uwazi_admin_agent.drivers.web.run_service import RunSummary


def _summary(run_id: str, created: datetime) -> RunSummary:
    return RunSummary(
        run_id=run_id,
        last_executed_at=None,
        status=RunStatus.PLANNED,
        created_at=created,
        prompt="p",
        modified=0,
        deleted=0,
        created=0,
        rewired=0,
        error=None,
    )


def test_rows_carry_a_numeric_creation_key_the_table_sorts_on() -> None:
    older = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    newer = older + timedelta(days=40)

    old_row = _summary_to_row(_summary("older", older), "http://instance")
    new_row = _summary_to_row(_summary("newer", newer), "http://instance")

    assert old_row["created"] == "2026-09-01 10:00"
    assert all(not isinstance(row["created_ts"], str) for row in (old_row, new_row))
    assert new_row["created_ts"] > old_row["created_ts"]


def test_an_in_flight_task_sorts_above_every_persisted_one() -> None:
    created = datetime.now(timezone.utc) - timedelta(days=1)
    persisted = _summary_to_row(_summary("done", created), "http://instance")

    assert _creating_to_row("being-made")["created_ts"] > persisted["created_ts"]
