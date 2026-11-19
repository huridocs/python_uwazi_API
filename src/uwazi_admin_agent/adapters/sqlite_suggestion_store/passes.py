# mypy: disable-error-code="attr-defined"
"""Suggestion-store module split out of ``sqlite_suggestion_store`` (behavior preserved)."""

from __future__ import annotations

from loguru import logger

from uwazi_admin_agent.domain.suggestion import (
    PredictionPass,
)

from .sql import _dt_to_text, _json_or_none, _row_to_pass


class PassesMixin:
    """Suggestion-store passes methods (moved verbatim from the original class)."""

    def save_pass(self, pass_row: PredictionPass) -> None:
        """Insert or overwrite the run's single pass row (progress ticks, terminal transitions)."""
        logger.info(
            "prediction pass start run={} scope={}",
            pass_row.run_id,
            "all" if pass_row.entity_scope is None else len(pass_row.entity_scope),
        )
        self._execute(
            """
            INSERT OR REPLACE INTO prediction_pass
                (run_id, status, budget, spent, entities_total, entities_done,
                 entity_scope, error, refused_commit, created_at, finished_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                pass_row.run_id,
                pass_row.status.value,
                _json_or_none(pass_row.budget.model_dump(mode="json")),
                _json_or_none(pass_row.spent.model_dump(mode="json")),
                pass_row.entities_total,
                pass_row.entities_done,
                _json_or_none(pass_row.entity_scope),
                pass_row.error,
                1 if pass_row.refused_commit else 0,
                _dt_to_text(pass_row.created_at),
                _dt_to_text(pass_row.finished_at),
            ),
        )

    def get_pass(self, run_id: str) -> PredictionPass | None:
        rows = self._query("SELECT * FROM prediction_pass WHERE run_id = ?", (run_id,))
        return _row_to_pass(rows[0]) if rows else None
