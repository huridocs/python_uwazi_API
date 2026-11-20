"""Row/SQL helpers shared by the suggestion-store mixins (moved verbatim)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from uwazi_admin_agent.domain.suggestion import (
    PredictionAttemptStatus,
    PredictionPass,
    Suggestion,
    SuggestionFilter,
    SuggestionStatus,
)

_TABLES = """
CREATE TABLE IF NOT EXISTS prediction_pass (
    run_id TEXT NOT NULL PRIMARY KEY,
    status TEXT NOT NULL,
    budget TEXT NOT NULL,
    spent TEXT NOT NULL,
    entities_total INTEGER NOT NULL DEFAULT 0,
    entities_done INTEGER NOT NULL DEFAULT 0,
    entity_scope TEXT,
    error TEXT,
    refused_commit INTEGER NOT NULL DEFAULT 0,
    created_at TEXT,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS suggestion (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    shared_id TEXT NOT NULL,
    entity_title TEXT NOT NULL DEFAULT '',
    template_name TEXT,
    op TEXT NOT NULL,
    language TEXT,
    property_name TEXT,
    detail_key TEXT,
    proposed_value TEXT,
    current_value TEXT,
    skip_reason TEXT,
    match_state INTEGER,
    merge_source_ids TEXT,
    merge_files TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    edited_value TEXT,
    rejected_values TEXT,
    selected_values TEXT,
    basis TEXT,
    context TEXT,
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


_INDEXES = """
CREATE UNIQUE INDEX IF NOT EXISTS suggestion_identity
    ON suggestion (
        run_id, shared_id, op,
        ifnull(language, ''), ifnull(property_name, ''), ifnull(detail_key, '')
    );

CREATE INDEX IF NOT EXISTS suggestion_review
    ON suggestion (run_id, status, match_state);

CREATE INDEX IF NOT EXISTS suggestion_entity_language
    ON suggestion (run_id, shared_id, language);
"""


APPLYABLE_STATUSES: tuple[str, ...] = (SuggestionStatus.ACCEPTED.value, SuggestionStatus.EDITED.value)


RUNNABLE_STATUSES: tuple[str, ...] = (
    SuggestionStatus.PENDING.value,
    SuggestionStatus.ACCEPTED.value,
    SuggestionStatus.EDITED.value,
    SuggestionStatus.REVERTED.value,
)


_OPERATION_SORT_FIELDS: dict[str, str] = {
    "recorded": "first_id",
    "modified": "modified_sort",
    "title": "title_sort",
    "template": "template_sort",
    "ops": "ops_sort",
    "status": "status_sort",
}


def operation_order_by(sort_by: str, sort_desc: bool) -> str:
    """The ORDER BY clause for one page of operations (pure; whitelisted fields).

    The sort control orders the WHOLE filtered set in SQL, so page flips keep
    the order rather than sorting one page at a time. Unknown values fall back
    to the recorded order, and every non-recorded order ties on ``first_id`` so
    two rows with the same title keep a stable page order. A row with no
    timestamp sorts last in either direction.
    """
    if sort_by not in _OPERATION_SORT_FIELDS or sort_by == "recorded":
        return f"first_id {'DESC' if sort_desc else 'ASC'}"
    direction = "DESC" if sort_desc else "ASC"
    field = _OPERATION_SORT_FIELDS[sort_by]
    if sort_by == "modified":
        return f"modified_sort IS NULL, modified_sort {direction}, first_id ASC"
    return f"{field} {direction}, first_id ASC"


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


def latest_review_activity(subs: list[Suggestion]) -> datetime | None:
    """The newest timestamp among one operation's rows (pure).

    A row's own "modified" moment is its last status change (``reviewed_at``)
    or, before anyone reviewed it, when it was recorded (``created_at``). The
    operation is the newest of its parts. The comparison is numeric
    (``timestamp()``) so a legacy NAIVE timestamp and a timezone-aware one can
    still be ordered together instead of raising ``TypeError``.
    """
    stamps = [sub.reviewed_at or sub.created_at for sub in subs]
    present = [stamp for stamp in stamps if stamp is not None]
    if not present:
        return None

    def _key(value: datetime) -> float:
        try:
            return value.timestamp()
        except (ValueError, OSError, OverflowError):
            return float("-inf")

    return max(present, key=_key)


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


def _pairs_by_language(
    pairs: list[tuple[str, str | None]],
) -> list[tuple[str | None, list[str]]]:
    """Group ``(sharedId, language)`` pairs by language, deduping ids (pure).

    Groups and ids keep their first-appearance order, so the generated clause
    and its bound parameters are deterministic.
    """
    grouped: dict[str | None, dict[str, None]] = {}
    for shared_id, language in pairs:
        grouped.setdefault(language, {}).setdefault(shared_id, None)
    return [(language, list(ids_by_shared_id)) for language, ids_by_shared_id in grouped.items()]


def _or_tree(terms: list[str]) -> str:
    """Join ``terms`` with OR as a BALANCED tree (pure).

    SQLite parses a flat ``a OR b OR c ...`` chain into a left-deep tree, so its
    depth grows with the number of terms and can exceed
    ``SQLITE_MAX_EXPR_DEPTH`` (1000 in stock builds), failing with "Expression
    tree is too large" before touching any row. The pair selection is already
    collapsed to one term per language by :func:`_pairs_by_language`; this keeps
    any many-term OR shallow (depth ``O(log n)``) while preserving order, so the
    bound parameters still line up with their placeholders.
    """
    if len(terms) == 1:
        return terms[0]
    mid = len(terms) // 2
    return f"({_or_tree(terms[:mid])} OR {_or_tree(terms[mid:])})"


def build_where(f: SuggestionFilter, run_id: str) -> tuple[str, list[Any]]:
    """Compose the parameterized WHERE clause for a filter (pure).

    Returns ``(sql, params)`` where ``sql`` starts with ``run_id = ?`` and
    appends only the predicates the filter actually sets. Every value is a bound
    parameter, so no filter input is ever interpolated into SQL.
    ``limit``/``offset`` are deliberately NOT part of the clause — the caller
    appends them, and the count/projection queries omit them.
    """
    clause = ["run_id = ?"]
    params: list[Any] = [run_id]
    if f.status is not None:
        if not f.status:
            clause.append("1 = 0")  # an explicit empty selection matches nothing
        else:
            clause.append(f"status IN ({', '.join('?' for _ in f.status)})")
            params.extend(s.value for s in f.status)
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
        if not f.op:
            clause.append("1 = 0")
        else:
            clause.append(f"op IN ({', '.join('?' for _ in f.op)})")
            params.extend(f.op)
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
        # language) pairs. Grouping them by language turns thousands of
        # ``(shared_id = ? AND language = ?)`` OR terms into ONE
        # ``language = ? AND shared_id IN (...)`` term per language: the clause
        # stays small, the ids use the shared_id index, and a whole-result
        # selection no longer builds a tree deep enough to trip SQLite's
        # expression-depth limit. A None language is the language-independent
        # group, hence IS NULL.
        if not f.entity_languages:
            clause.append("1 = 0")
        else:
            terms = []
            for language, shared_ids in _pairs_by_language(f.entity_languages):
                placeholders = ", ".join("?" for _ in shared_ids)
                if language is None:
                    terms.append(f"(language IS NULL AND shared_id IN ({placeholders}))")
                    params.extend(shared_ids)
                else:
                    terms.append(f"(language = ? AND shared_id IN ({placeholders}))")
                    params.append(language)
                    params.extend(shared_ids)
            clause.append(_or_tree(terms))
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
        shared_id=row["shared_id"],
        entity_title=row["entity_title"],
        template_name=row["template_name"],
        op=row["op"],
        language=row["language"],
        property_name=row["property_name"],
        detail_key=row["detail_key"],
        proposed_value=_value_from_json(row["proposed_value"]),
        current_value=_value_from_json(row["current_value"]),
        skip_reason=row["skip_reason"],
        match=None if match_state is None else bool(match_state),
        merge_source_ids=_value_from_json(row["merge_source_ids"]) or [],
        merge_files=_value_from_json(row["merge_files"]) or [],
        status=SuggestionStatus(row["status"]),
        edited_value=row["edited_value"],
        rejected_values=_value_from_json(row["rejected_values"]) or [],
        selected_values=_value_from_json(row["selected_values"]),
        basis=_value_from_json(row["basis"]) or [],
        context=row["context"],
        discard_reason=row["discard_reason"],
        reviewed_at=_dt_from_text(row["reviewed_at"]),
        created_at=_dt_from_text(row["created_at"]),
    )


def _row_to_pass(row: sqlite3.Row) -> PredictionPass:
    """Convert one ``prediction_pass`` row into the domain model."""
    return PredictionPass(
        run_id=row["run_id"],
        status=PredictionAttemptStatus(row["status"]),
        budget=_value_from_json(row["budget"]),
        spent=_value_from_json(row["spent"]),
        entities_total=row["entities_total"],
        entities_done=row["entities_done"],
        entity_scope=_value_from_json(row["entity_scope"]),
        error=row["error"],
        created_at=_dt_from_text(row["created_at"]),
        finished_at=_dt_from_text(row["finished_at"]),
        refused_commit=bool(row["refused_commit"]) if "refused_commit" in row.keys() else False,
    )
