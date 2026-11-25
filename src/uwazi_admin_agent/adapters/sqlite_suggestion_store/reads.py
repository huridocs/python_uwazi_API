# mypy: disable-error-code="attr-defined"
"""Suggestion-store module split out of ``sqlite_suggestion_store`` (behavior preserved)."""

from __future__ import annotations

import sqlite3
from typing import Any

from uwazi_admin_agent.domain.suggestion import (
    DISPLAY_ONLY_OPS,
    REVERT_RECREATES_OPS,
    EntityReviewRow,
    OperationReviewRow,
    Suggestion,
    SuggestionFilter,
    SuggestionStatus,
    entity_language_key,
)

from .sql import (
    RUNNABLE_STATUSES,
    _entity_fields,
    _row_to_suggestion,
    build_where,
    latest_review_activity,
    match_summary_of,
    operation_order_by,
)


class ReadsMixin:
    """Suggestion-store reads methods (moved verbatim from the original class)."""

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

    def query(self, run_id: str, f: SuggestionFilter) -> tuple[list[Suggestion], int]:
        where, params = build_where(f, run_id)
        total_rows = self._query(f"SELECT COUNT(*) AS n FROM suggestion WHERE {where}", params)
        total = int(total_rows[0]["n"]) if total_rows else 0
        page_rows = self._query(
            f"SELECT * FROM suggestion WHERE {where} ORDER BY id LIMIT ? OFFSET ?",
            [*params, f.limit, f.offset],
        )
        return [_row_to_suggestion(row) for row in page_rows], total

    def entity_rows(self, run_id: str, f: SuggestionFilter) -> tuple[list[EntityReviewRow], int]:
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
        where, params = build_where(f, run_id)
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

    def operations(self, run_id: str, f: SuggestionFilter) -> tuple[list[OperationReviewRow], int]:
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
        where, params = build_where(f, run_id)
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
                SUM(CASE WHEN is_validation = 1 THEN 1 ELSE 0 END) AS validation,
                COUNT(DISTINCT match_state) AS verdicts,
                SUM(CASE WHEN match_state IS NULL
                          AND op IN ('update','create','update_and_delete','create_and_delete')
                          AND proposed_value IS NOT NULL
                     THEN 1 ELSE 0 END) AS verdict_rows,
                MAX(CASE WHEN match_state = 0 THEN 1 ELSE 0 END) AS has_mismatch,
                MAX(CASE WHEN op IN ('update','create','update_and_delete','create_and_delete')
                              AND proposed_value IS NOT NULL THEN 1 ELSE 0 END)
                    AS has_outcome,
                LOWER(MAX(entity_title)) AS title_sort,
                LOWER(MAX(template_name)) AS template_sort,
                MIN(op) AS ops_sort,
                CASE
                    WHEN COUNT(*) = SUM(CASE WHEN status = 'applied' THEN 1 ELSE 0 END) THEN 'applied'
                    WHEN COUNT(*) = SUM(CASE WHEN status = 'reverted' THEN 1 ELSE 0 END) THEN 'reverted'
                    WHEN COUNT(*) = SUM(CASE WHEN status = 'discarded' THEN 1 ELSE 0 END) THEN 'discarded'
                    ELSE 'pending'
                END AS status_sort,
                MAX(julianday(COALESCE(reviewed_at, created_at))) AS modified_sort
            FROM suggestion
            WHERE {where}
            GROUP BY shared_id
            ORDER BY {operation_order_by(f.sort_by, f.sort_desc)}
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
                    validation=int(row["validation"] or 0),
                    match_summary=match_summary_of(verdicts, verdict_rows, bool(row["has_mismatch"])),
                    has_outcome=bool(row["has_outcome"]),
                    modified_at=latest_review_activity(subs),
                    subs=subs,
                )
            )
        return rows, total

    def languages(self, run_id: str) -> list[str]:
        """The DISTINCT languages the run's rows are written in (SQL).

        The Language filter's options: a language that only exists as a
        suboperation of some entity still has to be selectable, so this reads
        the rows, not the operation groups. Language-independent rows carry no
        language and are not listed — they are their own group, not a language.
        """
        rows = self._query(
            "SELECT DISTINCT language FROM suggestion WHERE run_id = ? AND language IS NOT NULL ORDER BY language",
            (run_id,),
        )
        return [str(row["language"]) for row in rows]

    def op_status_counts(self, run_id: str) -> dict[str, dict[str, int]]:
        """Row counts per ``op`` and per ``status`` over the whole run (SQL).

        The Op and Status filter options carry how many rows each value would
        match. Counting rows (not operation groups) matches what those filters
        select — they narrow the table's SUBOPERATIONS. One grouped pass per
        dimension over the run's rows; no per-page Python loop.
        """
        op_rows = self._query(
            "SELECT op, COUNT(*) AS n FROM suggestion WHERE run_id = ? GROUP BY op",
            (run_id,),
        )
        status_rows = self._query(
            "SELECT status, COUNT(*) AS n FROM suggestion WHERE run_id = ? GROUP BY status",
            (run_id,),
        )
        return {
            "op": {str(row["op"]): int(row["n"]) for row in op_rows},
            "status": {str(row["status"]): int(row["n"]) for row in status_rows},
        }

    def templates(self, run_id: str) -> list[str]:
        """The DISTINCT template names the run's rows belong to (SQL).

        The Property filter and the suboperation headings show a property's
        human LABEL, which belongs to its template; this list is what the page
        resolves those labels through. Rows with no template (a language-
        independent whole-entity op recorded before hydration) are skipped.
        """
        rows = self._query(
            "SELECT DISTINCT template_name FROM suggestion "
            "WHERE run_id = ? AND template_name IS NOT NULL ORDER BY template_name",
            (run_id,),
        )
        return [str(row["template_name"]) for row in rows]

    def selection_rows(
        self,
        run_id: str,
        f: SuggestionFilter,
        *,
        runnable_only: bool = False,
    ) -> list[tuple[int, str, str | None, str]]:
        """Every matching row's ``(id, sharedId, language, op)`` — the selection projection (SQL).

        The table's whole-result selection ("select all results") spans every
        page, so the Run/Discard/Revert actions cannot read the identity from
        the page payload. This is a projection, not a page: limit/offset are
        deliberately ignored, exactly like :meth:`shared_ids`. Display-only
        ``info`` rows are excluded: they can never be selected or run.
        ``runnable_only`` narrows to the statuses a Run may write, so a
        whole-result selection cannot include rows that are already applied or
        discarded.
        """
        where, params = build_where(f, run_id)
        # Holdout rows are score evidence: no selection (Run, Discard, Revert)
        # ever includes one, whatever the caller's filter says.
        clause = f"WHERE {where} AND op NOT IN ({', '.join('?' for _ in DISPLAY_ONLY_OPS)}) AND is_validation = 0"
        query_params: list[Any] = [*params, *sorted(DISPLAY_ONLY_OPS)]
        if runnable_only:
            placeholders = ", ".join("?" for _ in RUNNABLE_STATUSES)
            clause += f" AND status IN ({placeholders})"
            query_params.extend(RUNNABLE_STATUSES)
            # A reverted delete/merge row is NOT runnable again: its revert
            # re-created an entity with a fresh sharedId, so the recorded row's
            # old id is dead. Keep it out of "select all results" too.
            blocked_ops = sorted(REVERT_RECREATES_OPS)
            blocked_placeholders = ", ".join("?" for _ in blocked_ops)
            clause += f" AND NOT (status = ? AND op IN ({blocked_placeholders}))"
            query_params.extend([SuggestionStatus.REVERTED.value, *blocked_ops])
        rows = self._query(f"SELECT id, shared_id, language, op FROM suggestion {clause} ORDER BY id", query_params)
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

    def get_entity(
        self,
        run_id: str,
        shared_id: str,
        language: str | None = None,
        f: SuggestionFilter | None = None,
    ) -> list[Suggestion]:
        """Every suggestion for one entity IN ONE LANGUAGE group (the entity window).

        The entity window opens a (entity, language) row, so it reads that
        group's rows and no other: a ``es`` row shows the ``es`` values only, and
        the language-independent group (``language=None`` — selects, titles,
        whole-entity ops) is read with ``language IS NULL``.

        ``f`` carries the review table's current filter, so the window lists the
        same properties the table does. The entity and language predicates are
        added on top of it; the filter's pagination window is ignored by
        :func:`build_where`.
        """
        where, params = build_where(f or SuggestionFilter(), run_id)
        params.append(shared_id)
        if language is None:
            clause = f"{where} AND shared_id = ? AND language IS NULL"
        else:
            clause = f"{where} AND shared_id = ? AND language = ?"
            params.append(language)
        rows = self._query(f"SELECT * FROM suggestion WHERE {clause} ORDER BY id", params)
        return [_row_to_suggestion(row) for row in rows]

    def property_names(self, run_id: str) -> list[str]:
        rows = self._query(
            """
            SELECT DISTINCT property_name FROM suggestion
             WHERE run_id = ? AND property_name IS NOT NULL
             ORDER BY property_name
            """,
            (run_id,),
        )
        return [str(row["property_name"]) for row in rows]

    def shared_ids(self, run_id: str, f: SuggestionFilter) -> list[str]:
        where, params = build_where(f, run_id)
        placeholders = ", ".join("?" for _ in DISPLAY_ONLY_OPS)
        rows = self._query(
            f"SELECT DISTINCT shared_id FROM suggestion WHERE {where} AND op NOT IN ({placeholders}) ORDER BY shared_id",
            [*params, *sorted(DISPLAY_ONLY_OPS)],
        )
        return [str(row["shared_id"]) for row in rows]

    def file_operation_rows(self, run_id: str) -> list[Suggestion]:
        """Rows carrying merge/move sources + their recorded files (see the port)."""
        rows = self._query(
            """
            SELECT * FROM suggestion
             WHERE run_id = ?
               AND (ifnull(merge_source_ids, '') != '' OR ifnull(merge_files, '') != '')
               AND status != ?
             ORDER BY id
            """,
            (
                run_id,
                SuggestionStatus.DISCARDED.value,
            ),
        )
        return [_row_to_suggestion(row) for row in rows]

    def entity_languages(self, run_id: str, f: SuggestionFilter) -> list[tuple[str, str | None]]:
        """Every (sharedId, language) pair matching a filter, in first-appearance order.

        The entity window's Previous/Next walk THIS list, not the distinct
        sharedIds: the table's rows are per language, so a translation task's
        entity is one stop per language and navigation must land on the next
        row, not the next entity. ``info`` rows are display-only and name no
        entity, so they are excluded.
        """
        where, params = build_where(f, run_id)
        rows = self._query(
            f"""
            SELECT shared_id, language, MIN(id) AS first_id
            FROM suggestion
            WHERE {where} AND op NOT IN ({", ".join("?" for _ in DISPLAY_ONLY_OPS)})
            GROUP BY shared_id, language
            ORDER BY first_id
            """,
            [*params, *sorted(DISPLAY_ONLY_OPS)],
        )
        return [(str(row["shared_id"]), row["language"]) for row in rows]

    def counts(self, run_id: str) -> dict[str, int]:
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
                WHERE run_id = ? AND op NOT IN ('info', 'matched', 'skipped') AND is_validation = 0
                GROUP BY shared_id, language, status
            )
            GROUP BY status
            """,
            (run_id,),
        )
        match_rows = self._query(
            """
            SELECT match_state, COUNT(*) AS n FROM suggestion
             WHERE run_id = ? AND op NOT IN ('matched', 'skipped') AND is_validation = 0
             GROUP BY match_state
            """,
            (run_id,),
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
            "SELECT COUNT(*) AS n FROM (SELECT 1 FROM suggestion WHERE run_id = ? "
            "AND op NOT IN ('info', 'matched', 'skipped') AND is_validation = 0 GROUP BY shared_id, language)",
            (run_id,),
        )
        counts["total"] = int(total_rows[0]["n"]) if total_rows else 0
        # The header cards count SUBOPERATIONS (the table's real content: one
        # proposal per property/language/link), so they get their own row-level
        # counters next to the entity-language group counts above. The operation
        # count is the distinct sharedId — the table's row total.
        counts["subs_total"] = 0
        for status in SuggestionStatus:
            counts[f"subs_{status.value}"] = 0
        counts["display_matched"] = 0
        counts["display_skipped"] = 0
        display_rows = self._query(
            """
            SELECT op, COUNT(*) AS n FROM suggestion
             WHERE run_id = ? AND op IN ('matched', 'skipped')
             GROUP BY op
            """,
            (run_id,),
        )
        for row in display_rows:
            counts[f"display_{row['op']}"] = int(row["n"])
        for row in self._query(
            "SELECT status, COUNT(*) AS n FROM suggestion "
            "WHERE run_id = ? AND op NOT IN ('info', 'matched', 'skipped') AND is_validation = 0 GROUP BY status",
            (run_id,),
        ):
            count = int(row["n"])
            counts["subs_total"] += count
            counts[f"subs_{row['status']}"] = count
        operation_rows = self._query(
            "SELECT COUNT(DISTINCT shared_id) AS n FROM suggestion "
            "WHERE run_id = ? AND op NOT IN ('info', 'matched', 'skipped') AND is_validation = 0",
            (run_id,),
        )
        counts["operations_total"] = int(operation_rows[0]["n"]) if operation_rows else 0
        # Result outcomes are display-only evidence, NOT runnable work. They get
        # their own counters so the header can say "192 matched / 256 skipped"
        # without inflating Pending/Operations. ``result_entities`` counts every
        # distinct entity the run's rows represent.
        result_rows = self._query(
            "SELECT COUNT(*) AS n FROM (SELECT 1 FROM suggestion WHERE run_id = ? GROUP BY shared_id)",
            (run_id,),
        )
        counts["result_entities"] = int(result_rows[0]["n"]) if result_rows else 0
        # The holdout set gets its OWN counters: its rows are evidence, not work,
        # so they must never inflate Pending/Operations/Applied. The card reads
        # "Validation 24" beside the run's real totals.
        validation_rows = self._query(
            "SELECT COUNT(*) AS rows, COUNT(DISTINCT shared_id) AS entities FROM suggestion "
            "WHERE run_id = ? AND is_validation = 1",
            (run_id,),
        )
        counts["validation_rows"] = int(validation_rows[0]["rows"]) if validation_rows else 0
        counts["validation_entities"] = int(validation_rows[0]["entities"]) if validation_rows else 0
        return counts
