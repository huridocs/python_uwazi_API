"""Narrow persistence seam for the drift use case.

The drift engine needs four things from the suggestion store: the run's entity
rows, its file-operation rows, its baseline, and its last check verdict. It
deliberately depends on this small protocol instead of the concrete SQLite
adapter, so the use case stays inside ``use_cases`` and can be exercised with an
in-memory implementation in tests.

``SqliteSuggestionStore`` already implements every method; no inheritance is
required (the protocol is structural).
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from uwazi_admin_agent.domain.drift import DriftBaselineRow, DriftStateRow, DriftStatus
from uwazi_admin_agent.domain.suggestion import EntityReviewRow, Suggestion, SuggestionFilter


class DriftStorePort(Protocol):
    """The persistence the drift check/sync needs from a suggestion store."""

    def entity_rows(self, run_id: str, f: SuggestionFilter) -> tuple[list[EntityReviewRow], int]:
        """One row per affected entity (used to find the run's dominant template)."""
        ...

    def entity_languages(self, run_id: str, f: SuggestionFilter) -> list[tuple[str, str | None]]:
        """Every ``(sharedId, language)`` the run covers (the probe's language set)."""
        ...

    def file_operation_rows(self, run_id: str) -> list[Suggestion]:
        """Rows carrying a recorded file snapshot (merge/move sources)."""
        ...

    def drift_baseline(self, run_id: str) -> list[DriftBaselineRow]:
        """The run's captured ``(sharedId, language) -> editDate`` rows."""
        ...

    def replace_drift_baseline(self, run_id: str, rows: list[DriftBaselineRow]) -> None:
        """Replace the whole baseline with one freshly captured snapshot."""
        ...

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
        """Upsert the last-check verdict."""
        ...

    def drift_state(self, run_id: str) -> DriftStateRow | None:
        """The last-check verdict, or ``None`` when never checked."""
        ...

    def drift_mark_stale(self, run_id: str) -> None:
        """Forget the verdict (a write invalidated it)."""
        ...


__all__ = ["DriftStorePort"]
