"""Stdlib-``sqlite3`` implementation of :class:`SuggestionStorePort`.

One database file (``SUGGESTIONS_DB_PATH``) holds every run's prediction
attempts and suggestions. SQLite is the right tool for the review UI's scale
requirement: filtering, ordering, pagination, and aggregation all happen in SQL,
so a 10 000-row set costs a 25-row page read and a couple of ``GROUP BY``
scans — never a Python loop over the whole set.

Concurrency: the UI calls from NiceGUI's event loop (reads) and from worker
threads (prediction passes write as they go), so the connection is opened with
``check_same_thread=False`` and every statement is serialized behind one
``threading.Lock``. WAL journalling keeps a long reader from blocking the
writer. Statements are parameterized throughout (no string interpolation of
values); only the WHERE clause's *shape* is composed, from the filter object.

The pure pieces — :func:`build_where` and the row↔model converters — are
unit-tested directly (``tests/test_suggestion_filter.py``).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from uwazi_admin_agent.domain.drift import (
    DriftBaselineRow,
    DriftStateRow,
    DriftStatus,
)
from uwazi_admin_agent.domain.suggestion import (
    EntityReviewRow,
    PredictionAttempt,
    PredictionAttemptStatus,
    Suggestion,
    SuggestionFilter,
    SuggestionStatus,
    entity_language_key,
)
from uwazi_admin_agent.ports.suggestion_store_port import SuggestionStorePort

_TABLES = """
CREATE TABLE IF NOT EXISTS prediction_attempt (
    run_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    status TEXT NOT NULL,
    budget TEXT NOT NULL,
    spent TEXT NOT NULL,
    entities_total INTEGER NOT NULL DEFAULT 0,
    entities_done INTEGER NOT NULL DEFAULT 0,
    entity_scope TEXT,
    error TEXT,
    created_at TEXT,
    finished_at TEXT,
    PRIMARY KEY (run_id, attempt)
);

CREATE TABLE IF NOT EXISTS suggestion (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    shared_id TEXT NOT NULL,
    entity_title TEXT NOT NULL DEFAULT '',
    template_name TEXT,
    op TEXT NOT NULL,
    language TEXT,
    property_name TEXT,
    proposed_value TEXT,
    current_value TEXT,
    match_state INTEGER,
    status TEXT NOT NULL DEFAULT 'pending',
    edited_value TEXT,
    discard_reason TEXT,
    reviewed_at TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS drift_baseline (
    run_id TEXT NOT NULL,
    shared_id TEXT NOT NULL,
    language TEXT NOT NULL DEFAULT '',
    edit_date INTEGER,
    title TEXT,
    captured_at TEXT,
    PRIMARY KEY (run_id, shared_id, language)
);

CREATE TABLE IF NOT EXISTS drift_state (
    run_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'unknown',
    baseline_max_edit_date INTEGER,
    live_count INTEGER,
    checked_at TEXT,
    checked_by TEXT,
    detail TEXT
);
"""

# Indexes are kept apart from the tables because they name the *migrated*
# columns: creating them before ``ensure_schema`` adds a missing column fails
# with "no such column: language" on a database created by an older build.
_INDEXES = """
CREATE UNIQUE INDEX IF NOT EXISTS suggestion_identity
    ON suggestion (run_id, attempt, shared_id, op, ifnull(language, ''), ifnull(property_name, ''));

CREATE INDEX IF NOT EXISTS suggestion_review
    ON suggestion (run_id, attempt, status, match_state);

CREATE INDEX IF NOT EXISTS suggestion_entity_language
    ON suggestion (run_id, attempt, shared_id, language);
"""

# Statuses the apply path pushes (accepted, or accepted-then-edited).
APPLYABLE_STATUSES: tuple[str, ...] = (SuggestionStatus.ACCEPTED.value, SuggestionStatus.EDITED.value)


def match_summary_of(verdicts: int, verdict_rows: int, has_mismatch: bool) -> str:
    """An entity group's match verdict label (pure).

    ``verdicts`` is COUNT(DISTINCT match_state) over the group's rows (NULLs are
    NOT distinct-counted by SQL), ``verdict_rows`` the count of property rows
    that actually carry a proposal to compare, ``has_mismatch`` whether any row
    carries ``match_state = 0``. A single distinct verdict is its label;
    more than one distinct verdict = "mixed"; nothing comparable = "n/a" (and a
    NULL-only group is "new" — the proposed value has no current counterpart).
    """
    if verdicts > 1:
        return "mixed"
    if verdicts == 1:
        return "mismatch" if has_mismatch else "exact"
    return "new" if verdict_rows else "n/a"


def _json_or_none(value: Any) -> str | None:
    """Serialize a value for a TEXT column (None stays SQL NULL)."""
    if value is None:
        return None
    return json.dumps(value)


def _value_from_json(text: str | None) -> Any:
    """Deserialize a TEXT column back to a value (NULL → None)."""
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _dt_to_text(value: datetime | None) -> str | None:
    """Serialize a datetime as ISO-8601 (sorted lexicographically)."""
    return value.isoformat() if value is not None else None


def _dt_from_text(text: str | None) -> datetime | None:
    """Parse an ISO-8601 datetime (tolerating a naive legacy value)."""
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def build_where(f: SuggestionFilter, run_id: str, attempt: int) -> tuple[str, list[Any]]:
    """Compose the parameterized WHERE clause for a filter (pure).

    Returns ``(sql, params)`` where ``sql`` starts with ``run_id = ? AND attempt
    = ?`` and appends only the predicates the filter actually sets. Every value
    is a bound parameter, so no filter input is ever interpolated into SQL.
    ``limit``/``offset`` are deliberately NOT part of the clause — the caller
    appends them, and the count/projection queries omit them.
    """
    clause = ["run_id = ?", "attempt = ?"]
    params: list[Any] = [run_id, attempt]
    if f.status is not None:
        clause.append("status = ?")
        params.append(f.status.value)
    if f.accepted_only:
        clause.append(f"status IN ({', '.join('?' for _ in APPLYABLE_STATUSES)})")
        params.extend(APPLYABLE_STATUSES)
    if f.op is not None:
        clause.append("op = ?")
        params.append(f.op)
    if f.property_name is not None:
        clause.append("property_name = ?")
        params.append(f.property_name)
    if f.match is not None:
        if f.match == "new":
            clause.append("match_state IS NULL")
        else:
            clause.append("match_state = ?")
            params.append(1 if f.match == "exact" else 0)
    if f.text:
        clause.append("(entity_title LIKE ? OR shared_id LIKE ?)")
        like = f"%{f.text}%"
        params.extend([like, like])
    if f.shared_ids is not None:
        if not f.shared_ids:
            clause.append("1 = 0")  # an explicit empty selection matches nothing
        else:
            clause.append(f"shared_id IN ({', '.join('?' for _ in f.shared_ids)})")
            params.extend(f.shared_ids)
    if f.entity_languages is not None:
        # The table's rows are per language, so the selection is (sharedId,
        # language) pairs: OR over pairs, each an AND of the two. A None
        # language means the language-independent group, hence IS NULL.
        if not f.entity_languages:
            clause.append("1 = 0")
        else:
            parts = []
            for shared_id, language in f.entity_languages:
                if language is None:
                    parts.append("(shared_id = ? AND language IS NULL)")
                else:
                    parts.append("(shared_id = ? AND language = ?)")
            clause.append(f"({' OR '.join(parts)})")
            for shared_id, language in f.entity_languages:
                params.append(shared_id)
                if language is not None:
                    params.append(language)
    return " AND ".join(clause), params


def _row_to_suggestion(row: sqlite3.Row) -> Suggestion:
    """Convert one ``suggestion`` row into the domain model."""
    match_state = row["match_state"]
    return Suggestion(
        id=int(row["id"]),
        run_id=row["run_id"],
        attempt=row["attempt"],
        shared_id=row["shared_id"],
        entity_title=row["entity_title"],
        template_name=row["template_name"],
        op=row["op"],
        language=row["language"],
        property_name=row["property_name"],
        proposed_value=_value_from_json(row["proposed_value"]),
        current_value=_value_from_json(row["current_value"]),
        match=None if match_state is None else bool(match_state),
        status=SuggestionStatus(row["status"]),
        edited_value=row["edited_value"],
        discard_reason=row["discard_reason"],
        reviewed_at=_dt_from_text(row["reviewed_at"]),
        created_at=_dt_from_text(row["created_at"]),
    )


def _row_to_attempt(row: sqlite3.Row) -> PredictionAttempt:
    """Convert one ``prediction_attempt`` row into the domain model."""
    return PredictionAttempt(
        run_id=row["run_id"],
        attempt=row["attempt"],
        status=PredictionAttemptStatus(row["status"]),
        budget=_value_from_json(row["budget"]),
        spent=_value_from_json(row["spent"]),
        entities_total=row["entities_total"],
        entities_done=row["entities_done"],
        entity_scope=_value_from_json(row["entity_scope"]),
        error=row["error"],
        created_at=_dt_from_text(row["created_at"]),
        finished_at=_dt_from_text(row["finished_at"]),
    )


class SqliteSuggestionStore(SuggestionStorePort):
    """SQLite-backed suggestion + prediction-attempt store."""

    def __init__(self, path: Path) -> None:
        self._path: Path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self.ensure_schema()

    # --- lifecycle -----------------------------------------------------------

    def ensure_schema(self) -> None:
        """Create the tables + indexes when missing (idempotent), and migrate.

        Two additive migrations, both safe on a live DB:

        - ``template_name`` (created before the Template column existed);
        - ``language`` — the language a per-language property's value is written
          in. Rows recorded before this read NULL, which is exactly the
          language-independent group, so a legacy run keeps one row per entity
          (with its multi-language value shown whole) until the next planning
          pass re-records it.

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
            # An index created before ``language`` joined it cannot express the
            # new identity, and CREATE INDEX IF NOT EXISTS would keep the old
            # one: drop and let _INDEXES recreate it.
            self._conn.execute("DROP INDEX IF EXISTS suggestion_identity")
            self._conn.executescript(_INDEXES)
            self._conn.commit()

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

    # --- attempts ------------------------------------------------------------

    def create_attempt(self, attempt: PredictionAttempt) -> None:
        logger.info(
            "prediction attempt start run={} attempt={} scope={}",
            attempt.run_id,
            attempt.attempt,
            "all" if attempt.entity_scope is None else len(attempt.entity_scope),
        )
        self._execute(
            """
            INSERT OR REPLACE INTO prediction_attempt
                (run_id, attempt, status, budget, spent, entities_total, entities_done,
                 entity_scope, error, created_at, finished_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                attempt.run_id,
                attempt.attempt,
                attempt.status.value,
                _json_or_none(attempt.budget.model_dump(mode="json")),
                _json_or_none(attempt.spent.model_dump(mode="json")),
                attempt.entities_total,
                attempt.entities_done,
                _json_or_none(attempt.entity_scope),
                attempt.error,
                _dt_to_text(attempt.created_at),
                _dt_to_text(attempt.finished_at),
            ),
        )

    def update_attempt(self, attempt: PredictionAttempt) -> None:
        self.create_attempt(attempt)  # same row, INSERT OR REPLACE

    def get_attempt(self, run_id: str, attempt: int) -> PredictionAttempt | None:
        rows = self._query("SELECT * FROM prediction_attempt WHERE run_id = ? AND attempt = ?", (run_id, attempt))
        return _row_to_attempt(rows[0]) if rows else None

    def latest_attempt(self, run_id: str) -> PredictionAttempt | None:
        rows = self._query("SELECT * FROM prediction_attempt WHERE run_id = ? ORDER BY attempt DESC LIMIT 1", (run_id,))
        return _row_to_attempt(rows[0]) if rows else None

    def list_attempts(self, run_id: str) -> list[PredictionAttempt]:
        rows = self._query("SELECT * FROM prediction_attempt WHERE run_id = ? ORDER BY attempt DESC", (run_id,))
        return [_row_to_attempt(row) for row in rows]

    # --- suggestions ---------------------------------------------------------

    def add_suggestions(self, suggestions: list[Suggestion]) -> None:
        """Upsert suggestion rows, preserving operator review state.

        The upsert refreshes the PROPOSAL columns (proposed/current/match/title)
        and leaves the REVIEW columns (status/edited_value/discard_reason/
        reviewed_at) untouched for an existing identity: a re-run over the same
        scope must not silently un-discard an operator's earlier decision.
        """
        if not suggestions:
            return
        rows = [
            (
                s.run_id,
                s.attempt,
                s.shared_id,
                s.entity_title,
                s.template_name,
                s.op,
                s.language,
                s.property_name,
                _json_or_none(s.proposed_value),
                _json_or_none(s.current_value),
                None if s.match is None else (1 if s.match else 0),
                s.status.value,
                s.edited_value,
                s.discard_reason,
                _dt_to_text(s.reviewed_at),
                _dt_to_text(s.created_at),
            )
            for s in suggestions
        ]
        logger.info("prediction suggestions upserted: {} row(s)", len(rows))
        with self._lock:
            self._conn.executemany(
                """
                INSERT INTO suggestion
                    (run_id, attempt, shared_id, entity_title, template_name, op, language, property_name,
                     proposed_value, current_value, match_state, status, edited_value,
                     discard_reason, reviewed_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (run_id, attempt, shared_id, op, ifnull(language, ''), ifnull(property_name, ''))
                DO UPDATE SET
                    entity_title = excluded.entity_title,
                    template_name = excluded.template_name,
                    proposed_value = excluded.proposed_value,
                    current_value = excluded.current_value,
                    match_state = excluded.match_state
                """,
                rows,
            )
            self._conn.commit()

    def query(self, run_id: str, attempt: int, f: SuggestionFilter) -> tuple[list[Suggestion], int]:
        where, params = build_where(f, run_id, attempt)
        total_rows = self._query(f"SELECT COUNT(*) AS n FROM suggestion WHERE {where}", params)
        total = int(total_rows[0]["n"]) if total_rows else 0
        page_rows = self._query(
            f"SELECT * FROM suggestion WHERE {where} ORDER BY id LIMIT ? OFFSET ?",
            [*params, f.limit, f.offset],
        )
        return [_row_to_suggestion(row) for row in page_rows], total

    def entity_rows(self, run_id: str, attempt: int, f: SuggestionFilter) -> tuple[list[EntityReviewRow], int]:
        """One aggregate row per (entity, language), SQL-grouped (the review table's read).

        One `GROUP BY shared_id, language` statement over the SAME ``build_where``
        clause computes the counts, the per-status sums, the match verdict and
        the would-be-value flag under the page's LIMIT/OFFSET; a second statement
        projects the DISTINCT ops/properties ONLY for the paged groups (an ``IN``
        over that page, never the whole set). Ordered by MIN(id) — first
        appearance in the selection scan's records. ``total`` is the distinct
        (shared_id, language) count over the WHOLE filter (paging ignored), so
        the grid reads "N rows" without a Python-side pass.

        The language is part of the key because a translation task updates the
        same entity in several languages and the operator reviews each one
        separately. ``language IS NULL`` is the language-independent group (a
        select, a title, a delete) and is deliberately NOT merged into the
        per-language groups: it is its own row, labelled "—".
        """
        where, params = build_where(f, run_id, attempt)
        count_rows = self._query(
            f"SELECT COUNT(*) AS n FROM (SELECT 1 FROM suggestion WHERE {where} GROUP BY shared_id, language)",
            params,
        )
        total = int(count_rows[0]["n"]) if count_rows else 0
        group_rows = self._query(
            f"""
            SELECT
                shared_id,
                language,
                MIN(id) AS first_id,
                MAX(entity_title) AS entity_title,
                COUNT(*) AS proposal_count,
                SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) AS accepted,
                SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) AS applied,
                SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) AS reverted,
                SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) AS discarded,
                COUNT(DISTINCT match_state) AS verdicts,
                SUM(CASE WHEN match_state IS NULL
                          AND op IN ('update','create') AND proposed_value IS NOT NULL
                     THEN 1 ELSE 0 END) AS verdict_rows,
                MAX(CASE WHEN match_state = 0 THEN 1 ELSE 0 END) AS has_mismatch,
                MAX(CASE WHEN op IN ('update','create') AND proposed_value IS NOT NULL THEN 1 ELSE 0 END)
                    AS has_outcome
            FROM suggestion
            WHERE {where}
            GROUP BY shared_id, language
            ORDER BY first_id
            LIMIT ? OFFSET ?
            """,
            [
                SuggestionStatus.PENDING.value,
                SuggestionStatus.ACCEPTED.value,
                SuggestionStatus.APPLIED.value,
                SuggestionStatus.REVERTED.value,
                SuggestionStatus.DISCARDED.value,
                *params,
                f.limit,
                f.offset,
            ],
        )
        if not group_rows:
            return [], total
        # The op/property projection covers ONLY this page's groups: the IN list
        # is the grouped page, so its size stays the page size even when the
        # whole filter selects thousands. Grouping the detail rows by
        # (shared_id, language) keeps the projection from leaking one language's
        # ops into another's row.
        page_ids = [str(row["shared_id"]) for row in group_rows]
        id_placeholders = ", ".join("?" for _ in page_ids)
        detail_rows = self._query(
            f"""
            SELECT shared_id, language, op, property_name, template_name
            FROM suggestion
            WHERE {where} AND shared_id IN ({id_placeholders})
            ORDER BY id
            """,
            [*params, *page_ids],
        )
        fields_by_id = self._detail_fields(detail_rows)
        rows: list[EntityReviewRow] = []
        for row in group_rows:
            shared_id = str(row["shared_id"])
            language = row["language"]
            key = entity_language_key(shared_id, language)
            verdicts = int(row["verdicts"] or 0)
            verdict_rows = int(row["verdict_rows"] or 0)
            rows.append(
                EntityReviewRow(
                    shared_id=shared_id,
                    language=language,
                    entity_title=str(row["entity_title"] or ""),
                    template_names=fields_by_id[key]["templates"],
                    ops=fields_by_id[key]["ops"],
                    properties=fields_by_id[key]["properties"],
                    proposal_count=int(row["proposal_count"]),
                    pending=int(row["pending"] or 0),
                    accepted=int(row["accepted"] or 0),
                    applied=int(row["applied"] or 0),
                    reverted=int(row["reverted"] or 0),
                    discarded=int(row["discarded"] or 0),
                    match_summary=match_summary_of(verdicts, verdict_rows, bool(row["has_mismatch"])),
                    has_outcome=bool(row["has_outcome"]),
                )
            )
        return rows, total

    def _detail_fields(self, detail_rows: list[sqlite3.Row]) -> dict[str, dict[str, list[str]]]:
        """First-appearance ops, templates, + non-null property names per (id, language) (pure)."""
        fields: dict[str, dict[str, list[str]]] = {}
        for detail in detail_rows:
            key = entity_language_key(str(detail["shared_id"]), detail["language"])
            entry = fields.setdefault(key, {"ops": [], "properties": [], "templates": []})
            op = str(detail["op"])
            if op not in entry["ops"]:
                entry["ops"].append(op)
            name = detail["property_name"]
            if name is not None and str(name) not in entry["properties"]:
                entry["properties"].append(str(name))
            template = detail["template_name"]
            if template is not None and str(template) not in entry["templates"]:
                entry["templates"].append(str(template))
        return fields

    def get_entity(self, run_id: str, attempt: int, shared_id: str, language: str | None = None) -> list[Suggestion]:
        """Every suggestion for one entity IN ONE LANGUAGE group (the entity window).

        The entity window opens a (entity, language) row, so it reads that
        group's rows and no other: a ``es`` row shows the ``es`` values only, and
        the language-independent group (``language=None`` — selects, titles,
        whole-entity ops) is read with ``language IS NULL``.
        """
        where, params = build_where(SuggestionFilter(), run_id, attempt)
        params.append(shared_id)
        if language is None:
            clause = f"{where} AND shared_id = ? AND language IS NULL"
        else:
            clause = f"{where} AND shared_id = ? AND language = ?"
            params.append(language)
        rows = self._query(f"SELECT * FROM suggestion WHERE {clause} ORDER BY id", params)
        return [_row_to_suggestion(row) for row in rows]

    def set_status(
        self,
        ids: list[int],
        status: SuggestionStatus,
        edited_value: str | None = None,
        reason: str | None = None,
    ) -> None:
        if not ids:
            return
        placeholders = ", ".join("?" for _ in ids)
        self._execute(
            f"""
            UPDATE suggestion
               SET status = ?, edited_value = ?, discard_reason = ?, reviewed_at = ?
             WHERE id IN ({placeholders})
            """,
            [status.value, edited_value, reason, _dt_to_text(datetime.now().astimezone()), *ids],
        )

    def set_status_by_filter(
        self,
        run_id: str,
        attempt: int,
        f: SuggestionFilter,
        status: SuggestionStatus,
        reason: str | None = None,
    ) -> int:
        where, params = build_where(f, run_id, attempt)
        cursor = self._execute(
            f"""
            UPDATE suggestion
               SET status = ?, discard_reason = ?, reviewed_at = ?
             WHERE {where}
            """,
            [status.value, reason, _dt_to_text(datetime.now().astimezone()), *params],
        )
        logger.info("bulk status update: {} row(s) -> {}", cursor.rowcount, status.value)
        return int(cursor.rowcount)

    def reset_statuses(self, run_id: str, attempt: int, ids: list[int]) -> None:
        if not ids:
            return
        placeholders = ", ".join("?" for _ in ids)
        now = _dt_to_text(datetime.now().astimezone())
        self._execute(
            f"""
            UPDATE suggestion
               SET status = ?, discard_reason = NULL, reviewed_at = ?
             WHERE id IN ({placeholders}) AND status = ?
            """,
            [SuggestionStatus.PENDING.value, now, *ids, SuggestionStatus.DISCARDED.value],
        )

    def mark_applied_reverted(self, run_id: str) -> int:
        """Mark the run's APPLIED rows REVERTED after a revert; return the count.

        A revert undid exactly what those rows wrote, so they are runnable again
        and the review table says so instead of leaving them reading "applied".
        EVERY attempt of the run is marked, not just the reviewed one: one
        revert undoes the run's whole accumulated manifest, across passes. Only
        APPLIED rows move — PENDING/ACCEPTED/DISCARDED rows were never written,
        and already-REVERTED rows stay REVERTED (idempotent across a repeat).
        """
        cursor = self._execute(
            """
            UPDATE suggestion
               SET status = ?, reviewed_at = ?
             WHERE run_id = ? AND status = ?
            """,
            [
                SuggestionStatus.REVERTED.value,
                _dt_to_text(datetime.now().astimezone()),
                run_id,
                SuggestionStatus.APPLIED.value,
            ],
        )
        logger.info("reverted {} applied suggestion row(s) for run={}", cursor.rowcount, run_id)
        return int(cursor.rowcount)

    def mark_applied_reverted_for(self, run_id: str, shared_ids: list[str]) -> int:
        """Mark APPLIED rows REVERTED for exactly these sharedIds; return the count.

        One per-entity restore rolled back EVERY language row of the entity, so
        all its APPLIED rows move — across languages and attempts. No
        sharedIds (an empty list) moves nothing.
        """
        if not shared_ids:
            return 0
        placeholders = ", ".join("?" for _ in shared_ids)
        cursor = self._execute(
            f"""
            UPDATE suggestion
               SET status = ?, reviewed_at = ?
             WHERE run_id = ? AND status = ? AND shared_id IN ({placeholders})
            """,
            [
                SuggestionStatus.REVERTED.value,
                _dt_to_text(datetime.now().astimezone()),
                run_id,
                SuggestionStatus.APPLIED.value,
                *shared_ids,
            ],
        )
        logger.info("reverted {} applied row(s) for {} of run={}", cursor.rowcount, shared_ids, run_id)
        return int(cursor.rowcount)

    def property_names(self, run_id: str, attempt: int) -> list[str]:
        rows = self._query(
            """
            SELECT DISTINCT property_name FROM suggestion
             WHERE run_id = ? AND attempt = ? AND property_name IS NOT NULL
             ORDER BY property_name
            """,
            (run_id, attempt),
        )
        return [str(row["property_name"]) for row in rows]

    def shared_ids(self, run_id: str, attempt: int, f: SuggestionFilter) -> list[str]:
        where, params = build_where(f, run_id, attempt)
        rows = self._query(f"SELECT DISTINCT shared_id FROM suggestion WHERE {where} ORDER BY shared_id", params)
        return [str(row["shared_id"]) for row in rows]

    def entity_languages(self, run_id: str, attempt: int, f: SuggestionFilter) -> list[tuple[str, str | None]]:
        """Every (sharedId, language) pair matching a filter, in first-appearance order.

        The entity window's Previous/Next walk THIS list, not the distinct
        sharedIds: the table's rows are per language, so a translation task's
        entity is one stop per language and navigation must land on the next
        row, not the next entity.
        """
        where, params = build_where(f, run_id, attempt)
        rows = self._query(
            f"""
            SELECT shared_id, language, MIN(id) AS first_id
            FROM suggestion
            WHERE {where}
            GROUP BY shared_id, language
            ORDER BY first_id
            """,
            params,
        )
        return [(str(row["shared_id"]), row["language"]) for row in rows]

    def counts(self, run_id: str, attempt: int) -> dict[str, int]:
        """The review header's counters: entities per status (SQL-side group-bys).

        The status keys count ENTITY-LANGUAGE GROUPS, exactly the rows the table
        below the cards shows (``GROUP BY shared_id, language``, the same grouping
        :meth:`entity_rows` uses): ``total`` is the distinct (sharedId, language)
        count and each status is how many of those groups carry at least one row
        in it. Counting suggestion ROWS instead made one entity's 17 property
        rows read as "Total 17" above a table holding a single entity, and every
        status card inflated by the same factor. A group whose rows span statuses
        is therefore counted in each of them — the same round-trip the operator
        gets by picking that status in the filter dropdown.

        The ``match_*`` keys stay ROW-level: they are per-property comparison
        verdicts feeding the accuracy figure, which has no entity-level
        equivalent (one entity's rows can disagree — see
        :func:`match_summary_of`).
        """
        status_rows = self._query(
            """
            SELECT status, COUNT(*) AS n FROM (
                SELECT shared_id, language, status
                FROM suggestion
                WHERE run_id = ? AND attempt = ?
                GROUP BY shared_id, language, status
            )
            GROUP BY status
            """,
            (run_id, attempt),
        )
        match_rows = self._query(
            """
            SELECT match_state, COUNT(*) AS n FROM suggestion
             WHERE run_id = ? AND attempt = ?
             GROUP BY match_state
            """,
            (run_id, attempt),
        )
        counts: dict[str, int] = {status.value: 0 for status in SuggestionStatus}
        for row in status_rows:
            counts[str(row["status"])] = int(row["n"])
        counts["match_exact"] = 0
        counts["match_mismatch"] = 0
        counts["match_new"] = 0
        for row in match_rows:
            state = row["match_state"]
            if state is None:
                counts["match_new"] += int(row["n"])
            elif int(state) == 1:
                counts["match_exact"] += int(row["n"])
            else:
                counts["match_mismatch"] += int(row["n"])
        total_rows = self._query(
            "SELECT COUNT(*) AS n FROM (SELECT 1 FROM suggestion WHERE run_id = ? AND attempt = ? "
            "GROUP BY shared_id, language)",
            (run_id, attempt),
        )
        counts["total"] = int(total_rows[0]["n"]) if total_rows else 0
        return counts

    def delete_run(self, run_id: str) -> None:
        self._execute("DELETE FROM suggestion WHERE run_id = ?", (run_id,))
        self._execute("DELETE FROM prediction_attempt WHERE run_id = ?", (run_id,))
        self._execute("DELETE FROM drift_baseline WHERE run_id = ?", (run_id,))
        self._execute("DELETE FROM drift_state WHERE run_id = ?", (run_id,))

    def rename_run(self, old_id: str, new_id: str) -> None:
        """Re-key every row of ``old_id`` to ``new_id`` (see the port's contract).

        The backup store renames the run's folder; the review table, attempts and
        drift rows live in this shared database and are keyed by ``run_id``.
        Renaming only the folder left the new run's review page empty and the old
        id's rows orphaned. All four tables move together inside one locked
        transaction, so a crash cannot leave half the run on each id.
        """
        if old_id == new_id:
            return
        with self._lock:
            try:
                for table in ("suggestion", "prediction_attempt", "drift_baseline", "drift_state"):
                    self._conn.execute(f"UPDATE {table} SET run_id = ? WHERE run_id = ?", (new_id, old_id))
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        logger.info("renamed suggestion-store rows run={} -> {}", old_id, new_id)

    # --- drift baseline & status -------------------------------------------

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
        """Forget the last verdict (a new apply/attempt makes it out of date)."""
        self._execute("DELETE FROM drift_state WHERE run_id = ?", (run_id,))


__all__ = ["APPLYABLE_STATUSES", "SqliteSuggestionStore", "build_where"]
