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
from uwazi_admin_agent.domain.review_refresh import (
    ReconciliationSummary,
    identity_of,
    missing_status,
    reconciled_status,
    same_proposal,
    suggestion_identity,
)
from uwazi_admin_agent.domain.suggestion import (
    EntityReviewRow,
    OperationReviewRow,
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
    detail_key TEXT,
    proposed_value TEXT,
    current_value TEXT,
    match_state INTEGER,
    merge_source_ids TEXT,
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
    ON suggestion (
        run_id, attempt, shared_id, op,
        ifnull(language, ''), ifnull(property_name, ''), ifnull(detail_key, '')
    );

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


def _entity_fields(subs: list[Suggestion]) -> dict[str, list[str]]:
    """One operation's ops / templates / properties, first-appearance order (pure).

    The page-sized suboperation rows ARE the source: no second projection query
    is needed, and the order mirrors the rows the table shows. ``properties``
    skips whole-entity rows (a delete writes no property) and keeps ``_title``,
    which is a real, filterable row.
    """
    ops: list[str] = []
    templates: list[str] = []
    properties: list[str] = []
    for sub in subs:
        if sub.op not in ops:
            ops.append(sub.op)
        if sub.template_name and sub.template_name not in templates:
            templates.append(sub.template_name)
        if sub.property_name is not None and sub.property_name not in properties:
            properties.append(sub.property_name)
    return {"ops": ops, "templates": templates, "properties": properties}


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
    elif not f.include_obsolete:
        # A refresh retires rows in bulk; hiding them by default keeps the
        # table focused on work that can still run. Selecting the OBSOLETE
        # status explicitly is the way to inspect them.
        clause.append("status != ?")
        params.append(SuggestionStatus.OBSOLETE.value)
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
    if f.languages is not None:
        # The table's Language filter names the languages a SUBOPERATION is
        # written in; the language-independent rows belong to no language, so
        # they are not part of this selection.
        if not f.languages:
            clause.append("1 = 0")
        else:
            clause.append(f"language IN ({', '.join('?' for _ in f.languages)})")
            params.extend(f.languages)
    if f.suggestion_ids is not None:
        # The SUBOPERATION selection: the review table's checked parts are
        # suggestion row ids, so the bulk Run/Discard/Restore scopes to exactly
        # those rows (not to their entity-language siblings).
        if not f.suggestion_ids:
            clause.append("1 = 0")
        else:
            clause.append(f"id IN ({', '.join('?' for _ in f.suggestion_ids)})")
            params.extend(f.suggestion_ids)
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
        detail_key=row["detail_key"],
        proposed_value=_value_from_json(row["proposed_value"]),
        current_value=_value_from_json(row["current_value"]),
        match=None if match_state is None else bool(match_state),
        merge_source_ids=_value_from_json(row["merge_source_ids"]) or [],
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

        Additive migrations, all safe on a live DB:

        - ``template_name`` (created before the Template column existed);
        - ``language`` — the language a per-language property's value is written
          in. Rows recorded before this read NULL, which is exactly the
          language-independent group, so a legacy run keeps one row per entity
          (with its multi-language value shown whole) until the next planning
          pass re-records it.
        - ``merge_source_ids`` — the source entities a merge row deletes. Legacy
          rows read NULL, which the model turns into an empty list (no merge).
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
            if "detail_key" not in columns:
                self._conn.execute("ALTER TABLE suggestion ADD COLUMN detail_key TEXT")
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
                s.detail_key,
                _json_or_none(s.proposed_value),
                _json_or_none(s.current_value),
                None if s.match is None else (1 if s.match else 0),
                _json_or_none(s.merge_source_ids) if s.merge_source_ids else None,
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
                     detail_key, proposed_value, current_value, match_state, merge_source_ids, status,
                     edited_value, discard_reason, reviewed_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (run_id, attempt, shared_id, op, ifnull(language, ''), ifnull(property_name, ''),
                             ifnull(detail_key, ''))
                DO UPDATE SET
                    entity_title = excluded.entity_title,
                    template_name = excluded.template_name,
                    proposed_value = excluded.proposed_value,
                    current_value = excluded.current_value,
                    match_state = excluded.match_state,
                    merge_source_ids = excluded.merge_source_ids
                """,
                rows,
            )
            self._conn.commit()

    def reconcile_suggestions(
        self,
        run_id: str,
        attempt: int,
        suggestions: list[Suggestion],
    ) -> ReconciliationSummary:
        """Merge a refreshed script pass into the SAME attempt, in place.

        Unlike :meth:`add_suggestions` (which preserves every status on an
        identity match), a refresh also reacts to the proposal CHANGING and to
        rows the script no longer produces. The pure rules live in
        :mod:`uwazi_admin_agent.domain.review_refresh`; this method applies
        them in one transaction:

        - new identity → insert PENDING;
        - same proposal → keep the operator's status, refresh the proposal
          columns (including ``current_value``);
        - changed proposal → PENDING unless the row is EDITED/DISCARDED;
        - missing → APPLIED stays APPLIED, everything else becomes OBSOLETE;
        - a returning OBSOLETE row → PENDING.
        """
        existing = self._query("SELECT * FROM suggestion WHERE run_id = ? AND attempt = ?", (run_id, attempt))
        incoming_by_key: dict[tuple[str, str, str, str, str], Suggestion] = {}
        for suggestion in suggestions:
            incoming_by_key[identity_of(suggestion)] = suggestion
        existing_by_key = {
            suggestion_identity(
                str(row["shared_id"]),
                str(row["op"]),
                row["language"],
                row["property_name"],
                row["detail_key"],
            ): row
            for row in existing
        }
        now_text = _dt_to_text(datetime.now().astimezone())
        inserts: list[Suggestion] = []
        updates: list[tuple[Any, ...]] = []
        refreshed = status_reset = reappeared = 0
        for key, suggestion in incoming_by_key.items():
            old = existing_by_key.get(key)
            if old is None:
                inserts.append(suggestion)
                continue
            old_status = SuggestionStatus(str(old["status"]))
            same = same_proposal(_value_from_json(old["proposed_value"]), suggestion.proposed_value)
            status = reconciled_status(
                old_status,
                same=same,
                reappearing=old_status is SuggestionStatus.OBSOLETE,
            )
            if old_status is SuggestionStatus.OBSOLETE:
                reappeared += 1
            if status is SuggestionStatus.PENDING and status is not old_status:
                status_reset += 1
            edited_value = old["edited_value"] if status is SuggestionStatus.EDITED else None
            discard_reason = old["discard_reason"] if status is SuggestionStatus.DISCARDED else None
            reviewed_at = old["reviewed_at"] if status is old_status else now_text
            old_match = old["match_state"]
            new_match = None if suggestion.match is None else (1 if suggestion.match else 0)
            if (
                not same
                or old["entity_title"] != suggestion.entity_title
                or old["template_name"] != suggestion.template_name
                or _value_from_json(old["current_value"]) != suggestion.current_value
                or old_match != new_match
            ):
                refreshed += 1
            updates.append(
                (
                    suggestion.entity_title,
                    suggestion.template_name,
                    _json_or_none(suggestion.proposed_value),
                    _json_or_none(suggestion.current_value),
                    new_match,
                    _json_or_none(suggestion.merge_source_ids) if suggestion.merge_source_ids else None,
                    status.value,
                    edited_value,
                    discard_reason,
                    reviewed_at,
                    int(old["id"]),
                )
            )

        obsolete = kept_applied = 0
        for key, old in existing_by_key.items():
            if key in incoming_by_key:
                continue
            old_status = SuggestionStatus(str(old["status"]))
            status = missing_status(old_status)
            if status is SuggestionStatus.OBSOLETE:
                obsolete += 1
            else:
                kept_applied += 1
            if status is not old_status or status is SuggestionStatus.OBSOLETE:
                updates.append(
                    (
                        old["entity_title"],
                        old["template_name"],
                        old["proposed_value"],
                        old["current_value"],
                        old["match_state"],
                        old["merge_source_ids"],
                        status.value,
                        None if status is not SuggestionStatus.EDITED else old["edited_value"],
                        None if status is not SuggestionStatus.DISCARDED else old["discard_reason"],
                        now_text,
                        int(old["id"]),
                    )
                )

        if updates:
            with self._lock:
                self._conn.executemany(
                    """
                    UPDATE suggestion
                       SET entity_title = ?, template_name = ?, proposed_value = ?,
                           current_value = ?, match_state = ?, merge_source_ids = ?,
                           status = ?, edited_value = ?, discard_reason = ?, reviewed_at = ?
                     WHERE id = ?
                    """,
                    updates,
                )
                self._conn.commit()
        if inserts:
            self.add_suggestions(inserts)
        summary = ReconciliationSummary(
            inserted=len(inserts),
            refreshed=refreshed,
            status_reset=status_reset,
            reappeared=reappeared,
            obsolete=obsolete,
            kept_applied=kept_applied,
        )
        logger.info(
            "table refresh merged run={} attempt={}: {}",
            run_id,
            attempt,
            summary.summary_text(),
        )
        return summary

    def set_current_values(self, updates: list[tuple[int, Any, bool | None]]) -> None:
        """Refresh rows' before-state in place after a write or a revert (bulk).

        ``updates`` carries ``(row id, live value, match)``. ``match=None`` is
        the "nothing to compare" case (a removed value / a new one), matching
        the prediction hydration rule.
        """
        if not updates:
            return
        with self._lock:
            self._conn.executemany(
                "UPDATE suggestion SET current_value = ?, match_state = ? WHERE id = ?",
                [
                    (_json_or_none(value), None if match is None else (1 if match else 0), row_id)
                    for row_id, value, match in updates
                ],
            )
            self._conn.commit()

    def get_suggestion(self, suggestion_id: int) -> Suggestion | None:
        """One suggestion row by id (the suboperation undo reads it before restoring)."""
        rows = self._query("SELECT * FROM suggestion WHERE id = ?", (suggestion_id,))
        return _row_to_suggestion(rows[0]) if rows else None

    def applied_rows_for(self, run_id: str, shared_ids: list[str] | None = None) -> list[Suggestion]:
        """Every still-APPLIED row of a run (optionally narrowed to sharedIds).

        Used by revert to update ``current_value`` for exactly the rows whose
        writes are being undone. An empty explicit list returns nothing.
        """
        if shared_ids is not None and not shared_ids:
            return []
        clause = "run_id = ? AND status = ?"
        params: list[Any] = [run_id, SuggestionStatus.APPLIED.value]
        if shared_ids is not None:
            placeholders = ", ".join("?" for _ in shared_ids)
            clause += f" AND shared_id IN ({placeholders})"
            params.extend(shared_ids)
        rows = self._query(f"SELECT * FROM suggestion WHERE {clause} ORDER BY id", params)
        return [_row_to_suggestion(row) for row in rows]

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
                          AND op IN ('update','create','update_and_delete','create_and_delete')
                          AND proposed_value IS NOT NULL
                     THEN 1 ELSE 0 END) AS verdict_rows,
                MAX(CASE WHEN match_state = 0 THEN 1 ELSE 0 END) AS has_mismatch,
                MAX(CASE WHEN op IN ('update','create','update_and_delete','create_and_delete')
                              AND proposed_value IS NOT NULL THEN 1 ELSE 0 END)
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

    def operations(self, run_id: str, attempt: int, f: SuggestionFilter) -> tuple[list[OperationReviewRow], int]:
        """One row per ENTITY (the review table's operation) + its suboperations, SQL-grouped.

        The table's row is the entity — one sharedId, however many languages its
        suboperations write — so the grouping key is ``shared_id`` alone. One
        ``GROUP BY shared_id`` statement over the SAME ``build_where`` clause
        computes the counts and the match verdict under the page's
        LIMIT/OFFSET; a second statement then loads the page's SUBOPERATION rows
        (the ``Suggestion`` models themselves) with that same clause. Because
        the filter is a SUBOPERATION filter, an operation whose every part the
        filter removed is absent from both queries — the "no suboperations left
        means no row" rule, enforced in SQL rather than by a Python pass.

        ``total`` is the distinct-sharedId count over the WHOLE filter (paging
        ignored), so the grid reads "N operations" without loading the set.
        Bounded work: the detail statement only ever covers the page's entities.
        """
        where, params = build_where(f, run_id, attempt)
        count_rows = self._query(
            f"SELECT COUNT(*) AS n FROM (SELECT 1 FROM suggestion WHERE {where} GROUP BY shared_id)",
            params,
        )
        total = int(count_rows[0]["n"]) if count_rows else 0
        if total == 0:
            return [], 0
        group_rows = self._query(
            f"""
            SELECT
                shared_id,
                MIN(id) AS first_id,
                MAX(entity_title) AS entity_title,
                COUNT(*) AS proposal_count,
                SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) AS accepted,
                SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) AS applied,
                SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) AS reverted,
                SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) AS discarded,
                SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) AS edited,
                COUNT(DISTINCT match_state) AS verdicts,
                SUM(CASE WHEN match_state IS NULL
                          AND op IN ('update','create','update_and_delete','create_and_delete')
                          AND proposed_value IS NOT NULL
                     THEN 1 ELSE 0 END) AS verdict_rows,
                MAX(CASE WHEN match_state = 0 THEN 1 ELSE 0 END) AS has_mismatch,
                MAX(CASE WHEN op IN ('update','create','update_and_delete','create_and_delete')
                              AND proposed_value IS NOT NULL THEN 1 ELSE 0 END)
                    AS has_outcome
            FROM suggestion
            WHERE {where}
            GROUP BY shared_id
            ORDER BY first_id
            LIMIT ? OFFSET ?
            """,
            [
                SuggestionStatus.PENDING.value,
                SuggestionStatus.ACCEPTED.value,
                SuggestionStatus.APPLIED.value,
                SuggestionStatus.REVERTED.value,
                SuggestionStatus.DISCARDED.value,
                SuggestionStatus.EDITED.value,
                *params,
                f.limit,
                f.offset,
            ],
        )
        if not group_rows:
            return [], total
        page_ids = [str(row["shared_id"]) for row in group_rows]
        id_placeholders = ", ".join("?" for _ in page_ids)
        detail_rows = self._query(
            f"""
            SELECT * FROM suggestion
            WHERE {where} AND shared_id IN ({id_placeholders})
            ORDER BY id
            """,
            [*params, *page_ids],
        )
        subs_by_entity: dict[str, list[Suggestion]] = {}
        for detail in detail_rows:
            subs_by_entity.setdefault(str(detail["shared_id"]), []).append(_row_to_suggestion(detail))
        rows: list[OperationReviewRow] = []
        for row in group_rows:
            shared_id = str(row["shared_id"])
            subs = subs_by_entity.get(shared_id, [])
            fields = _entity_fields(subs)
            verdicts = int(row["verdicts"] or 0)
            verdict_rows = int(row["verdict_rows"] or 0)
            rows.append(
                OperationReviewRow(
                    shared_id=shared_id,
                    entity_title=str(row["entity_title"] or ""),
                    template_names=fields["templates"],
                    ops=fields["ops"],
                    properties=fields["properties"],
                    languages=sorted({sub.language for sub in subs if sub.language}),
                    proposal_count=int(row["proposal_count"]),
                    pending=int(row["pending"] or 0),
                    accepted=int(row["accepted"] or 0),
                    applied=int(row["applied"] or 0),
                    reverted=int(row["reverted"] or 0),
                    discarded=int(row["discarded"] or 0),
                    edited=int(row["edited"] or 0),
                    match_summary=match_summary_of(verdicts, verdict_rows, bool(row["has_mismatch"])),
                    has_outcome=bool(row["has_outcome"]),
                    subs=subs,
                )
            )
        return rows, total

    def languages(self, run_id: str, attempt: int) -> list[str]:
        """The DISTINCT languages the attempt's rows are written in (SQL).

        The Language filter's options: a language that only exists as a
        suboperation of some entity still has to be selectable, so this reads
        the rows, not the operation groups. Language-independent rows carry no
        language and are not listed — they are their own group, not a language.
        """
        rows = self._query(
            "SELECT DISTINCT language FROM suggestion "
            "WHERE run_id = ? AND attempt = ? AND language IS NOT NULL ORDER BY language",
            (run_id, attempt),
        )
        return [str(row["language"]) for row in rows]

    def templates(self, run_id: str, attempt: int) -> list[str]:
        """The DISTINCT template names the attempt's rows belong to (SQL).

        The Property filter and the suboperation headings show a property's
        human LABEL, which belongs to its template; this list is what the page
        resolves those labels through. Rows with no template (a language-
        independent whole-entity op recorded before hydration) are skipped.
        """
        rows = self._query(
            "SELECT DISTINCT template_name FROM suggestion "
            "WHERE run_id = ? AND attempt = ? AND template_name IS NOT NULL ORDER BY template_name",
            (run_id, attempt),
        )
        return [str(row["template_name"]) for row in rows]

    def property_scores(self, run_id: str, attempt: int) -> dict[str, tuple[int, int]]:
        """``property -> (exact, mismatch)`` verdict counts for the whole attempt (SQL).

        The Property filter's options carry each property's accuracy (the same
        figure the entity window shows), and it must span the WHOLE run, not the
        page: a per-page accuracy would change as the operator pages or filters.
        A ``new`` value has no current counterpart to be right or wrong about,
        so it is left out of both counters by construction (NULL match_state).
        """
        rows = self._query(
            """
            SELECT property_name,
                   SUM(CASE WHEN match_state = 1 THEN 1 ELSE 0 END) AS exact,
                   SUM(CASE WHEN match_state = 0 THEN 1 ELSE 0 END) AS mismatch
              FROM suggestion
             WHERE run_id = ? AND attempt = ? AND property_name IS NOT NULL
             GROUP BY property_name
            """,
            (run_id, attempt),
        )
        return {str(row["property_name"]): (int(row["exact"] or 0), int(row["mismatch"] or 0)) for row in rows}

    def selection_rows(self, run_id: str, attempt: int, f: SuggestionFilter) -> list[tuple[int, str, str | None, str]]:
        """Every matching row's ``(id, sharedId, language, op)`` — the selection projection (SQL).

        The table's whole-result selection ("select all results") spans every
        page, so the Run/Discard/Revert actions cannot read the identity from
        the page payload. This is a projection, not a page: limit/offset are
        deliberately ignored, exactly like :meth:`shared_ids`.
        """
        where, params = build_where(f, run_id, attempt)
        rows = self._query(
            f"SELECT id, shared_id, language, op FROM suggestion WHERE {where} ORDER BY id",
            params,
        )
        return [(int(row["id"]), str(row["shared_id"]), row["language"], str(row["op"])) for row in rows]

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

    def discard_by_filter(
        self,
        run_id: str,
        attempt: int,
        f: SuggestionFilter,
        reason: str | None = None,
    ) -> int:
        """Mark the filter's rows DISCARDED (the ones not written yet); return the count.

        One statement, so the bulk Discard over a whole selection costs a single
        UPDATE rather than a Python loop over ids. The APPLIED rows are left
        alone on purpose: they are already in Uwazi, and a discard says what
        must NOT be executed — un-writing is the revert's job. Flipping them
        would also hide them from the run's revert, which finds its rows by
        that status.
        """
        where, params = build_where(f, run_id, attempt)
        cursor = self._execute(
            f"""
            UPDATE suggestion
               SET status = ?, discard_reason = ?, reviewed_at = ?
             WHERE {where} AND status != ?
            """,
            [
                SuggestionStatus.DISCARDED.value,
                reason,
                _dt_to_text(datetime.now().astimezone()),
                *params,
                SuggestionStatus.APPLIED.value,
            ],
        )
        logger.info("discarded {} suggestion row(s) for run={}", cursor.rowcount, run_id)
        return int(cursor.rowcount)

    def restore_by_filter(self, run_id: str, attempt: int, f: SuggestionFilter) -> int:
        """Return the filter's DISCARDED rows to PENDING (undo a discard); return the count.

        The counterpart of :meth:`discard_by_filter`: an operation the operator
        marked as discarded goes back to the review queue. Rows in any other
        status keep theirs, so an undo never resurrects an applied write.
        """
        where, params = build_where(f, run_id, attempt)
        cursor = self._execute(
            f"""
            UPDATE suggestion
               SET status = ?, discard_reason = NULL, reviewed_at = ?
             WHERE {where} AND status = ?
            """,
            [
                SuggestionStatus.PENDING.value,
                _dt_to_text(datetime.now().astimezone()),
                *params,
                SuggestionStatus.DISCARDED.value,
            ],
        )
        logger.info("restored {} discarded suggestion row(s) for run={}", cursor.rowcount, run_id)
        return int(cursor.rowcount)

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
            "AND status != ? GROUP BY shared_id, language)",
            (run_id, attempt, SuggestionStatus.OBSOLETE.value),
        )
        counts["total"] = int(total_rows[0]["n"]) if total_rows else 0
        # The header cards count SUBOPERATIONS (the table's real content: one
        # proposal per property/language/link), so they get their own row-level
        # counters next to the entity-language group counts above. The operation
        # count is the distinct sharedId — the table's row total.
        counts["subs_total"] = 0
        for status in SuggestionStatus:
            counts[f"subs_{status.value}"] = 0
        for row in self._query(
            "SELECT status, COUNT(*) AS n FROM suggestion WHERE run_id = ? AND attempt = ? GROUP BY status",
            (run_id, attempt),
        ):
            count = int(row["n"])
            if row["status"] != SuggestionStatus.OBSOLETE.value:
                counts["subs_total"] += count
            counts[f"subs_{row['status']}"] = count
        operation_rows = self._query(
            "SELECT COUNT(DISTINCT shared_id) AS n FROM suggestion WHERE run_id = ? AND attempt = ? AND status != ?",
            (run_id, attempt, SuggestionStatus.OBSOLETE.value),
        )
        counts["operations_total"] = int(operation_rows[0]["n"]) if operation_rows else 0
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
