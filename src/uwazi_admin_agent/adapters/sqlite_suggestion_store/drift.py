# mypy: disable-error-code="attr-defined"
"""Suggestion-store module split out of ``sqlite_suggestion_store`` (behavior preserved)."""

from __future__ import annotations

from datetime import datetime

from uwazi_admin_agent.domain.drift import (
    DriftBaselineRow,
    DriftStateRow,
    DriftStatus,
)


class DriftMixin:
    """Suggestion-store drift methods (moved verbatim from the original class)."""

    def replace_drift_baseline(self, run_id: str, rows: list[DriftBaselineRow]) -> None:
        """Replace the run's whole baseline with ``rows`` (one sync = one pass)."""
        with self._lock:
            self._conn.execute("DELETE FROM drift_baseline WHERE run_id = ?", (run_id,))
            self._conn.executemany(
                """
                INSERT INTO drift_baseline (run_id, shared_id, language, edit_date, title, captured_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_id,
                        row.shared_id,
                        row.language or "",
                        row.edit_date,
                        row.title,
                        self._dt_text(row.captured_at),
                    )
                    for row in rows
                ],
            )
            self._conn.commit()

    def drift_baseline(self, run_id: str) -> list[DriftBaselineRow]:
        """The run's baseline rows (insert order is meaningless; compare by key)."""
        rows = self._query("SELECT * FROM drift_baseline WHERE run_id = ?", (run_id,))
        return [
            DriftBaselineRow(
                run_id=str(row["run_id"]),
                shared_id=str(row["shared_id"]),
                language=row["language"] or None,
                edit_date=int(row["edit_date"]) if row["edit_date"] is not None else None,
                title=row["title"],
                captured_at=self._dt_from_text(row["captured_at"]),
            )
            for row in rows
        ]

    def set_drift_state(
        self,
        run_id: str,
        *,
        status: DriftStatus,
        checked_at: datetime,
        checked_by: str,
        baseline_max_edit_date: int | None = None,
        live_count: int | None = None,
        detail: str | None = None,
    ) -> None:
        """Upsert the run's last-check outcome (the banner reads this)."""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO drift_state (run_id, status, baseline_max_edit_date, live_count, checked_at, checked_by, detail)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    status = excluded.status,
                    baseline_max_edit_date = excluded.baseline_max_edit_date,
                    live_count = excluded.live_count,
                    checked_at = excluded.checked_at,
                    checked_by = excluded.checked_by,
                    detail = excluded.detail
                """,
                (
                    run_id,
                    status.value,
                    baseline_max_edit_date,
                    live_count,
                    self._dt_text(checked_at),
                    checked_by,
                    detail,
                ),
            )
            self._conn.commit()

    def drift_state(self, run_id: str) -> DriftStateRow | None:
        """The run's last-check outcome, or None when never checked."""
        rows = self._query("SELECT * FROM drift_state WHERE run_id = ?", (run_id,))
        if not rows:
            return None
        row = rows[0]
        return DriftStateRow(
            run_id=str(row["run_id"]),
            status=DriftStatus(row["status"]),
            baseline_max_edit_date=int(row["baseline_max_edit_date"]) if row["baseline_max_edit_date"] is not None else None,
            live_count=int(row["live_count"]) if row["live_count"] is not None else None,
            checked_at=self._dt_from_text(row["checked_at"]),
            checked_by=row["checked_by"],
            detail=row["detail"],
        )

    def drift_mark_stale(self, run_id: str) -> None:
        """Forget the last verdict (a new apply/pass makes it out of date)."""
        self._execute("DELETE FROM drift_state WHERE run_id = ?", (run_id,))
