# mypy: disable-error-code="attr-defined"
"""Suggestion-store module split out of ``sqlite_suggestion_store`` (behavior preserved)."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from .sql import _INDEXES, _TABLES


class BaseMixin:
    """Suggestion-store base methods (moved verbatim from the original class)."""

    def __init__(self, path: Path) -> None:
        self._path: Path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self.ensure_schema()

    def ensure_schema(self) -> None:
        """Create the tables + indexes when missing (idempotent), and migrate.

        Additive migrations, all safe on a live DB:

        - ``template_name`` (created before the Template column existed);
        - ``language`` — the language a per-language property's value is written
          in. Rows recorded before this read NULL, which is exactly the
          language-independent group, so a legacy run keeps one row per entity
          (with its multi-language value shown whole) until the next planning
          pass re-records it.
        - ``merge_source_ids`` — the source entities a merge row deletes. Legacy
          rows read NULL, which the model turns into an empty list (no merge).
        - ``merge_files`` — the source files a merge row would copy onto its
          target, recorded by the dry run for display. Legacy rows read NULL,
          which the model turns into an empty list (the review then falls back
          to a per-source move row).
        - ``detail_key`` — what distinguishes two rows that are otherwise the
          same change. It is the link target for a ``create_relationships`` row,
          so a batch of links is one row PER LINK instead of one row carrying
          the whole batch (and each link can be checked, run and discarded on
          its own). Legacy rows read NULL, which keys them exactly as before.

        The ORDER is load-bearing: tables, then the ALTERs, then the indexes.
        ``suggestion_identity`` is recreated on every pass because it cannot
        express a column added later — and ``suggestion_entity_language`` names
        ``language`` directly, so on a database an older build created (no such
        column yet) running the index script first dies with "no such column:
        language", failing the whole store construction.

        ``ALTER TABLE`` keeps a NULL default, so legacy rows migrate without a
        table rewrite and their grouped queries read NULL exactly as intended.
        """
        with self._lock:
            self._conn.executescript(_TABLES)
            columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(suggestion)")}
            if "template_name" not in columns:
                self._conn.execute("ALTER TABLE suggestion ADD COLUMN template_name TEXT")
            if "language" not in columns:
                self._conn.execute("ALTER TABLE suggestion ADD COLUMN language TEXT")
            if "merge_source_ids" not in columns:
                self._conn.execute("ALTER TABLE suggestion ADD COLUMN merge_source_ids TEXT")
            if "merge_files" not in columns:
                self._conn.execute("ALTER TABLE suggestion ADD COLUMN merge_files TEXT")
            if "detail_key" not in columns:
                self._conn.execute("ALTER TABLE suggestion ADD COLUMN detail_key TEXT")
            if "rejected_values" not in columns:
                self._conn.execute("ALTER TABLE suggestion ADD COLUMN rejected_values TEXT")
            if "skip_reason" not in columns:
                self._conn.execute("ALTER TABLE suggestion ADD COLUMN skip_reason TEXT")
            pass_columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(prediction_pass)")}
            if pass_columns and "refused_commit" not in pass_columns:
                self._conn.execute("ALTER TABLE prediction_pass ADD COLUMN refused_commit INTEGER NOT NULL DEFAULT 0")
            self._migrate_attempts_to_single_pass()
            # An index created before ``language`` joined it cannot express the
            # new identity, and CREATE INDEX IF NOT EXISTS would keep the old
            # one: drop and let _INDEXES recreate it.
            self._conn.execute("DROP INDEX IF EXISTS suggestion_identity")
            self._conn.executescript(_INDEXES)
            self._conn.commit()

    def _migrate_attempts_to_single_pass(self) -> None:
        """Migrate a pre-attempt-removal database to the one-live-table shape.

        The ORDER is load-bearing:

        1. the old indexes NAME the ``attempt`` column, so they must go before
           the column does;
        2. only the NEWEST attempt's rows per run survive — the live table is
           the newest pass's rows;
        3. ``ALTER TABLE ... DROP COLUMN attempt`` (the column is in no key);
        4. ``prediction_attempt`` is REBUILT as ``prediction_pass`` (its
           primary key contained the attempt number, so the column could not
           simply be dropped), keeping the newest pass per run.

        Idempotent: a database already in the new shape has neither the column
        nor the old table, so the whole block is skipped. A fresh database
        never enters it (the new tables come from ``_TABLES``).
        """
        tables = {row["name"] for row in self._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(suggestion)")}
        if "attempt" in columns:
            self._conn.execute("DROP INDEX IF EXISTS suggestion_identity")
            self._conn.execute("DROP INDEX IF EXISTS suggestion_review")
            self._conn.execute("DROP INDEX IF EXISTS suggestion_entity_language")
            self._conn.execute(
                "DELETE FROM suggestion WHERE attempt NOT IN (SELECT MAX(attempt) FROM suggestion GROUP BY run_id)"
            )
            self._conn.execute("ALTER TABLE suggestion DROP COLUMN attempt")
        if "prediction_attempt" in tables:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS prediction_pass (
                    run_id TEXT NOT NULL PRIMARY KEY,
                    status TEXT NOT NULL,
                    budget TEXT NOT NULL,
                    spent TEXT NOT NULL,
                    entities_total INTEGER NOT NULL DEFAULT 0,
                    entities_done INTEGER NOT NULL DEFAULT 0,
                    entity_scope TEXT,
                    error TEXT,
                    created_at TEXT,
                    finished_at TEXT
                );
                INSERT OR REPLACE INTO prediction_pass
                    SELECT run_id, status, budget, spent, entities_total, entities_done,
                           entity_scope, error, 0, created_at, finished_at
                      FROM prediction_attempt
                     WHERE attempt = (SELECT MAX(p2.attempt) FROM prediction_attempt p2
                                       WHERE p2.run_id = prediction_attempt.run_id);
                DROP TABLE prediction_attempt;
                """
            )

    def close(self) -> None:
        """Close the connection (tests; the web app keeps it open for its lifetime)."""
        with self._lock:
            self._conn.close()

    def _execute(self, sql: str, params: list[Any] | tuple[Any, ...] = ()) -> sqlite3.Cursor:
        """Run one statement under the lock and commit."""
        with self._lock:
            cursor = self._conn.execute(sql, params)
            self._conn.commit()
            return cursor

    def _query(self, sql: str, params: list[Any] | tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        """Run one read under the lock and return every row."""
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    def _dt_text(self, value: datetime | None) -> str | None:
        """Serialize a datetime for a TEXT column (None stays SQL NULL)."""
        return value.isoformat() if value is not None else None

    def _dt_from_text(self, text: str | None) -> datetime | None:
        """Deserialize a TEXT-column datetime (SQL NULL → None; bad value → None)."""
        if text is None:
            return None
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return None
