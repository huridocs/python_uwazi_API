# mypy: disable-error-code="attr-defined"
"""Suggestion-store module split out of ``sqlite_suggestion_store`` (behavior preserved)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from loguru import logger

from uwazi_admin_agent.domain.review_refresh import (
    ReconciliationSummary,
    identity_of,
    matched_swap_pairs,
    missing_status,
    property_row_key,
    reconciled_status,
    same_proposal,
    suggestion_identity,
)
from uwazi_admin_agent.domain.select_values import ALL_VALUES_REJECTED_REASON
from uwazi_admin_agent.domain.suggestion import (
    INFO_OP,
    MATCHED_OP,
    PROPERTY_OPS,
    REVERT_RECREATES_OPS,
    Suggestion,
    SuggestionFilter,
    SuggestionStatus,
)

from .sql import _dt_to_text, _json_or_none, _value_from_json, build_where


def _reconciled_values(old: Any, suggestion: Suggestion, now_text: str) -> tuple[tuple[Any, ...], dict[str, bool]]:
    """The UPDATE bindings for one stored row that survives a refresh (pure-ish).

    Shared by the identity match and by the matched/update swap, so both apply
    exactly the same status rules. The tuple order must match the UPDATE
    statement in ``reconcile_suggestions``.
    """
    old_status = SuggestionStatus(str(old["status"]))
    same = same_proposal(_value_from_json(old["proposed_value"]), suggestion.proposed_value)
    status = reconciled_status(
        old_status,
        same=same,
        reappearing=old_status is SuggestionStatus.OBSOLETE,
        # A delete/merge undo re-created an entity, so the reverted row
        # could not be run again. The refresh has just re-read the
        # operation from live Uwazi (and replaced a merge's source ids),
        # so it is safe to put it back in the runnable queue.
        rerun_after_refresh=str(old["op"]) in REVERT_RECREATES_OPS,
    )
    # A display-only outcome is not work the operator can accept or run: coming
    # back to MATCHED drops any pending review decision (APPLIED, a write that
    # really happened, stays as history). Promoting a MATCHED row to a real
    # change puts it back in the review queue.
    if suggestion.op == MATCHED_OP:
        status = SuggestionStatus.APPLIED if old_status is SuggestionStatus.APPLIED else SuggestionStatus.PENDING
    elif str(old["op"]) == MATCHED_OP:
        status = SuggestionStatus.PENDING
    edited_value = old["edited_value"] if status is SuggestionStatus.EDITED else None
    discard_reason = old["discard_reason"] if status is SuggestionStatus.DISCARDED else None
    reviewed_at = old["reviewed_at"] if status is old_status else now_text
    old_rejected = _value_from_json(old["rejected_values"]) or []
    # Same proposal keeps the operator's value-level choices; a changed
    # proposal clears them (never carry stale tokens into a new list).
    rejected_values: list[str] = old_rejected if same else []
    old_selected = _value_from_json(old["selected_values"])
    # The explicit selection is operator state like the rejections: a changed
    # proposal clears it, the same proposal keeps it.
    selected_values: list[str] | None = old_selected if same else None
    if not same and old_status is SuggestionStatus.DISCARDED:
        # A whole-row discard left by the value mechanism only means
        # "nothing left to keep", not a real refusal: the refresh puts
        # the new proposal back into review.
        if old["discard_reason"] == ALL_VALUES_REJECTED_REASON:
            status = SuggestionStatus.PENDING
            discard_reason = None
    old_match = old["match_state"]
    new_match = None if suggestion.match is None else (1 if suggestion.match else 0)
    refreshed = (
        not same
        or old["entity_title"] != suggestion.entity_title
        or old["template_name"] != suggestion.template_name
        or _value_from_json(old["current_value"]) != suggestion.current_value
        or old["skip_reason"] != suggestion.skip_reason
        or old_match != new_match
        or (_value_from_json(old["merge_files"]) or []) != suggestion.merge_files
        or str(old["op"]) != suggestion.op
        or old["detail_key"] != suggestion.detail_key
        or (_value_from_json(old["basis"]) or []) != suggestion.basis
        or old["context"] != suggestion.context
        or old["source_shared_id"] != suggestion.source_shared_id
    )
    values = (
        suggestion.op,
        suggestion.detail_key,
        suggestion.entity_title,
        suggestion.template_name,
        _json_or_none(suggestion.proposed_value),
        _json_or_none(suggestion.current_value),
        suggestion.skip_reason,
        new_match,
        _json_or_none(suggestion.merge_source_ids) if suggestion.merge_source_ids else None,
        _json_or_none(suggestion.merge_files) if suggestion.merge_files else None,
        status.value,
        edited_value,
        discard_reason,
        _json_or_none(rejected_values) if rejected_values else None,
        _json_or_none(selected_values) if selected_values is not None else None,
        _json_or_none(suggestion.basis) if suggestion.basis else None,
        suggestion.context,
        suggestion.source_shared_id,
        reviewed_at,
        int(old["id"]),
    )
    return values, {
        "refreshed": refreshed,
        "status_reset": status is SuggestionStatus.PENDING and status is not old_status,
        "reappeared": old_status is SuggestionStatus.OBSOLETE,
    }


class WritesMixin:
    """Suggestion-store writes methods (moved verbatim from the original class)."""

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
                s.shared_id,
                s.entity_title,
                s.template_name,
                s.op,
                s.language,
                s.property_name,
                s.detail_key,
                _json_or_none(s.proposed_value),
                _json_or_none(s.current_value),
                s.skip_reason,
                None if s.match is None else (1 if s.match else 0),
                _json_or_none(s.merge_source_ids) if s.merge_source_ids else None,
                _json_or_none(s.merge_files) if s.merge_files else None,
                s.status.value,
                s.edited_value,
                _json_or_none(s.rejected_values) if s.rejected_values else None,
                _json_or_none(s.selected_values) if s.selected_values is not None else None,
                _json_or_none(s.basis) if s.basis else None,
                s.context,
                s.source_shared_id,
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
                    (run_id, shared_id, entity_title, template_name, op, language, property_name,
                     detail_key, proposed_value, current_value, skip_reason, match_state, merge_source_ids,
                     merge_files, status, edited_value, rejected_values, selected_values, basis,
                     context, source_shared_id, discard_reason, reviewed_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (run_id, shared_id, op, ifnull(language, ''), ifnull(property_name, ''),
                             ifnull(detail_key, ''))
                DO UPDATE SET
                    entity_title = excluded.entity_title,
                    template_name = excluded.template_name,
                    proposed_value = excluded.proposed_value,
                    current_value = excluded.current_value,
                    skip_reason = excluded.skip_reason,
                    match_state = excluded.match_state,
                    merge_source_ids = excluded.merge_source_ids,
                    merge_files = excluded.merge_files,
                    basis = excluded.basis,
                    context = excluded.context,
                    source_shared_id = excluded.source_shared_id
                """,
                rows,
            )
            self._conn.commit()

    def reconcile_suggestions(
        self,
        run_id: str,
        suggestions: list[Suggestion],
        entity_scope: list[str] | None = None,
    ) -> ReconciliationSummary:
        """Merge a refreshed script pass into the SAME table, in place.

        Unlike :meth:`add_suggestions` (which preserves every status on an
        identity match), a refresh also reacts to the proposal CHANGING and to
        rows the script no longer produces. The pure rules live in
        :mod:`uwazi_admin_agent.domain.review_refresh`; this method applies
        them in one transaction:

        - new identity → insert PENDING;
        - same proposal → keep the operator's status, refresh the proposal
          columns (including ``current_value``);
        - changed proposal → PENDING unless the row is EDITED/DISCARDED;
        - a property that starts/stops matching Uwazi → the SAME row becomes the
          incoming ``update``/``matched`` row (one row, never an obsolete
          duplicate beside the new one);
        - missing → APPLIED stays APPLIED, everything else becomes OBSOLETE;
        - a returning OBSOLETE row → PENDING.

        ``entity_scope`` narrows the merge to the entities the pass covered
        (None = the whole run). A scope-limited pass says NOTHING about the
        entities it never visited, so their rows are left exactly as they are —
        only the rows of the covered entities can be retired. INFO rows are the
        exception: their synthetic ``info-N`` id names no entity, and the pass
        always re-decides them, so a scoped refresh can retire a stale info row
        instead of leaving it in the table forever.
        """
        scope = set(entity_scope) if entity_scope is not None else None
        existing = self._query("SELECT * FROM suggestion WHERE run_id = ?", (run_id,))
        if scope is not None:
            # INFO rows have synthetic ids that name no entity, and the pass
            # always re-decides them, so a scoped refresh can retire a stale one
            # instead of leaving it in the table forever. Matched/skipped rows
            # DO name real entities, so they follow the same scope rule as
            # changes: an unscoped entity's result stays untouched.
            existing = [row for row in existing if str(row["op"]) == INFO_OP or str(row["shared_id"]) in scope]
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

        # A property can flip between "already equal to Uwazi" (a display-only
        # MATCHED row) and a real change (an UPDATE row). Their store identities
        # differ, so without this pairing the merge would retire the matched row
        # and insert a duplicate update for the same property. Pair them and
        # rewrite the stored row in place instead: one row, the new op.
        swaps = matched_swap_pairs(
            [(key, str(row["op"]), row["language"], row["property_name"]) for key, row in existing_by_key.items()],
            [
                (key, suggestion.op, suggestion.language, suggestion.property_name)
                for key, suggestion in incoming_by_key.items()
            ],
        )
        swapped_existing = {old_key for old_key, _incoming_key in swaps}
        swapped_incoming = {incoming_key for _old_key, incoming_key in swaps}

        for old_key, incoming_key in swaps:
            values, meta = _reconciled_values(existing_by_key[old_key], incoming_by_key[incoming_key], now_text)
            updates.append(values)
            refreshed += int(meta["refreshed"])
            status_reset += int(meta["status_reset"])
            reappeared += int(meta["reappeared"])

        for key, suggestion in incoming_by_key.items():
            if key in swapped_incoming:
                continue
            old = existing_by_key.get(key)
            if old is None:
                inserts.append(suggestion)
                continue
            values, meta = _reconciled_values(old, suggestion, now_text)
            updates.append(values)
            refreshed += int(meta["refreshed"])
            status_reset += int(meta["status_reset"])
            reappeared += int(meta["reappeared"])

        # Legacy tables (refreshed before the swap rule existed) can already
        # hold an OBSOLETE matched row NEXT TO the update row for the same
        # property. The matched evidence is fully superseded, so drop it rather
        # than keep a phantom "obsolete"/"matched" count for work the table does
        # not show. Only an OBSOLETE matched row whose property the script now
        # reports as a real change is removed; anything the script no longer
        # produces keeps its normal refresh verdict.
        incoming_change_keys = {
            property_row_key(suggestion.shared_id, suggestion.language, suggestion.property_name)
            for suggestion in incoming_by_key.values()
            if suggestion.op in PROPERTY_OPS and suggestion.property_name is not None
        }
        stale_matched_ids: list[int] = []
        for key, old in existing_by_key.items():
            if key in swapped_existing or str(old["op"]) != MATCHED_OP:
                continue
            if SuggestionStatus(str(old["status"])) is not SuggestionStatus.OBSOLETE:
                continue
            if property_row_key(str(old["shared_id"]), old["language"], old["property_name"]) in incoming_change_keys:
                stale_matched_ids.append(int(old["id"]))
        stale_matched = set(stale_matched_ids)

        obsolete = kept_applied = 0
        for key, old in existing_by_key.items():
            if key in incoming_by_key or key in swapped_existing or int(old["id"]) in stale_matched:
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
                        old["op"],
                        old["detail_key"],
                        old["entity_title"],
                        old["template_name"],
                        old["proposed_value"],
                        old["current_value"],
                        old["skip_reason"],
                        old["match_state"],
                        old["merge_source_ids"],
                        old["merge_files"],
                        status.value,
                        None if status is not SuggestionStatus.EDITED else old["edited_value"],
                        None if status is not SuggestionStatus.DISCARDED else old["discard_reason"],
                        _json_or_none(_value_from_json(old["rejected_values"]) or []),
                        old["selected_values"],
                        old["basis"],
                        old["context"],
                        old["source_shared_id"],
                        now_text,
                        int(old["id"]),
                    )
                )

        if updates or stale_matched_ids:
            with self._lock:
                if updates:
                    self._conn.executemany(
                        """
                        UPDATE suggestion
                           SET op = ?, detail_key = ?, entity_title = ?, template_name = ?,
                               proposed_value = ?, current_value = ?, skip_reason = ?, match_state = ?,
                               merge_source_ids = ?, merge_files = ?, status = ?, edited_value = ?,
                               discard_reason = ?, rejected_values = ?, selected_values = ?, basis = ?,
                               context = ?, source_shared_id = ?, reviewed_at = ?
                         WHERE id = ?
                        """,
                        updates,
                    )
                if stale_matched_ids:
                    self._conn.executemany(
                        "DELETE FROM suggestion WHERE id = ?",
                        [(suggestion_id,) for suggestion_id in stale_matched_ids],
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
            "table refresh merged run={} scope={}: {}",
            run_id,
            "all" if scope is None else len(scope),
            summary.summary_text(),
        )
        return summary

    def set_current_values(self, updates: list[tuple[int, Any, bool | None]]) -> None:
        """Refresh rows' before-state in place after a revert (bulk).

        ``updates`` carries ``(row id, live value, match)``. ``match=None`` is
        the "nothing to compare" case (a removed value / a new one), matching
        the prediction hydration rule.

        A Run deliberately does NOT use this: ``current_value`` is the row's
        BEFORE state, which the review table's "Value" column and the entity
        window show and which the Revert restores. Overwriting it with the
        written value would hide what an undo puts back.
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
               SET status = ?, edited_value = ?, discard_reason = ?, reviewed_at = ?,
                   rejected_values = CASE WHEN ? = 'edited' THEN NULL ELSE rejected_values END,
                   selected_values = CASE WHEN ? = 'edited' THEN NULL ELSE selected_values END
             WHERE id IN ({placeholders})
            """,
            [
                status.value,
                edited_value,
                reason,
                _dt_to_text(datetime.now().astimezone()),
                status.value,
                status.value,
                *ids,
            ],
        )

    def set_rejected_values(
        self,
        row_id: int,
        rejected: list[str],
        status: SuggestionStatus | None = None,
        reason: str | None = None,
    ) -> None:
        """Persist one row's rejected multiselect labels (and its derived status, when given)."""
        self._execute(
            """
            UPDATE suggestion
               SET rejected_values = ?, status = COALESCE(?, status), discard_reason = ?,
                   reviewed_at = ?
             WHERE id = ?
            """,
            [
                _json_or_none(rejected) if rejected else None,
                None if status is None else status.value,
                reason,
                _dt_to_text(datetime.now().astimezone()),
                row_id,
            ],
        )

    def set_selected_values(
        self,
        row_id: int,
        selected: list[str] | None,
        status: SuggestionStatus | None = None,
        reason: str | None = None,
    ) -> None:
        """Persist one multiselect row's explicit selection; clears the proposal rejections."""
        self._execute(
            """
            UPDATE suggestion
               SET selected_values = ?, rejected_values = NULL, status = COALESCE(?, status),
                   discard_reason = ?, reviewed_at = ?
             WHERE id = ?
            """,
            [
                _json_or_none(selected) if selected is not None else None,
                None if status is None else status.value,
                reason,
                _dt_to_text(datetime.now().astimezone()),
                row_id,
            ],
        )

    def set_status_by_filter(
        self,
        run_id: str,
        f: SuggestionFilter,
        status: SuggestionStatus,
        reason: str | None = None,
    ) -> int:
        where, params = build_where(f, run_id)
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

    def reset_statuses(self, run_id: str, ids: list[int]) -> None:
        if not ids:
            return
        placeholders = ", ".join("?" for _ in ids)
        now = _dt_to_text(datetime.now().astimezone())
        self._execute(
            f"""
            UPDATE suggestion
               SET status = ?, discard_reason = NULL, reviewed_at = ?,
                   rejected_values = CASE WHEN discard_reason = ? THEN NULL ELSE rejected_values END,
                   selected_values = CASE WHEN discard_reason = ? THEN NULL ELSE selected_values END
             WHERE id IN ({placeholders}) AND status = ?
            """,
            [
                SuggestionStatus.PENDING.value,
                now,
                ALL_VALUES_REJECTED_REASON,
                ALL_VALUES_REJECTED_REASON,
                *ids,
                SuggestionStatus.DISCARDED.value,
            ],
        )

    def discard_by_filter(
        self,
        run_id: str,
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
        where, params = build_where(f, run_id)
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

    def restore_by_filter(self, run_id: str, f: SuggestionFilter) -> int:
        """Return the filter's DISCARDED rows to PENDING (undo a discard); return the count.

        The counterpart of :meth:`discard_by_filter`: an operation the operator
        marked as discarded goes back to the review queue. Rows in any other
        status keep theirs, so an undo never resurrects an applied write.
        """
        where, params = build_where(f, run_id)
        cursor = self._execute(
            f"""
            UPDATE suggestion
               SET status = ?, discard_reason = NULL, reviewed_at = ?,
                   rejected_values = CASE WHEN discard_reason = ? THEN NULL ELSE rejected_values END,
                   selected_values = CASE WHEN discard_reason = ? THEN NULL ELSE selected_values END
             WHERE {where} AND status = ?
            """,
            [
                SuggestionStatus.PENDING.value,
                _dt_to_text(datetime.now().astimezone()),
                ALL_VALUES_REJECTED_REASON,
                ALL_VALUES_REJECTED_REASON,
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
        EVERY row of the run is marked: one revert undoes the run's whole
        accumulated manifest, across passes. Only
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
        all its APPLIED rows move — across languages. No
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

    def delete_run(self, run_id: str) -> None:
        self._execute("DELETE FROM suggestion WHERE run_id = ?", (run_id,))
        self._execute("DELETE FROM prediction_pass WHERE run_id = ?", (run_id,))
        self._execute("DELETE FROM drift_baseline WHERE run_id = ?", (run_id,))
        self._execute("DELETE FROM drift_state WHERE run_id = ?", (run_id,))

    def rename_run(self, old_id: str, new_id: str) -> None:
        """Re-key every row of ``old_id`` to ``new_id`` (see the port's contract).

        The backup store renames the run's folder; the review table, the pass and
        drift rows live in this shared database and are keyed by ``run_id``.
        Renaming only the folder left the new run's review page empty and the old
        id's rows orphaned. All four tables move together inside one locked
        transaction, so a crash cannot leave half the run on each id.
        """
        if old_id == new_id:
            return
        with self._lock:
            try:
                for table in ("suggestion", "prediction_pass", "drift_baseline", "drift_state"):
                    self._conn.execute(f"UPDATE {table} SET run_id = ? WHERE run_id = ?", (new_id, old_id))
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        logger.info("renamed suggestion-store rows run={} -> {}", old_id, new_id)
