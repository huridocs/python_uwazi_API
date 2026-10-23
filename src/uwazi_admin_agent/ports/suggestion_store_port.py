"""Persistence + query seam for a run's prediction pass and its suggestions.

The review UI's 10k-entity scale requirement lives here: every selection surface
(the grid's checkboxes, the bulk accept/discard buttons, the subset re-predict,
the subset apply) resolves to either an explicit id list or a
:class:`SuggestionFilter`, and this port's implementations do the filtering,
paginating, and aggregating in the store — never by loading every row into the
UI.

Two record families:

- :class:`PredictionPass` — ONE row per run, polled while the pass runs (progress)
  and read afterwards (spent vs budget, abort reason).
- :class:`Suggestion` — the reviewable rows, upserted idempotently on
  ``(run_id, shared_id, op, property_name)`` so re-running a pass over
  the same scope overwrites those rows instead of duplicating them.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from uwazi_admin_agent.domain.review_refresh import ReconciliationSummary
from uwazi_admin_agent.domain.suggestion import (
    EntityReviewRow,
    OperationReviewRow,
    PredictionPass,
    Suggestion,
    SuggestionFilter,
    SuggestionStatus,
)


class SuggestionStorePort(ABC):
    """Storage + query API for a run's prediction pass and its suggestions."""

    @abstractmethod
    def ensure_schema(self) -> None:
        """Create the tables/indexes when missing (idempotent; safe on every build)."""
        ...

    # --- the run's prediction pass -------------------------------------------

    @abstractmethod
    def save_pass(self, pass_row: PredictionPass) -> None:
        """Insert or overwrite the run's single pass row (progress ticks, terminal transitions)."""
        ...

    @abstractmethod
    def get_pass(self, run_id: str) -> PredictionPass | None:
        """The run's single pass row, or None."""
        ...

    # --- suggestions ---------------------------------------------------------

    @abstractmethod
    def add_suggestions(self, suggestions: list[Suggestion]) -> None:
        """Upsert suggestion rows (idempotent on run/shared_id/op/property).

        An upsert, not an insert: a re-run of the same pass over the same scope
        must overwrite those rows. Operator review state (status/edited value)
        is NOT clobbered by an upsert of the same key — the row is a prediction
        identity, so its proposal fields refresh while its review fields survive.
        """
        ...

    @abstractmethod
    def reconcile_suggestions(
        self,
        run_id: str,
        suggestions: list[Suggestion],
        entity_scope: list[str] | None = None,
    ) -> ReconciliationSummary:
        """Merge a refreshed pass into the SAME table, preserving only safe state.

        New identities insert PENDING; unchanged proposals keep the operator's
        status; changed proposals reset PENDING except EDITED/DISCARDED; rows
        the script no longer produces keep APPLIED or become OBSOLETE.

        ``entity_scope`` narrows the merge to the entities the pass actually
        covered (None = the whole run). A scope-limited pass does NOT retire the
        rows of the entities it never visited — it only says nothing about them.
        """
        ...

    @abstractmethod
    def set_current_values(self, updates: list[tuple[int, Any, bool | None]]) -> None:
        """Refresh rows' live before-state after an apply/revert/table refresh."""
        ...

    @abstractmethod
    def get_suggestion(self, suggestion_id: int) -> Suggestion | None:
        """One row by id, or None."""
        ...

    @abstractmethod
    def applied_rows_for(self, run_id: str, shared_ids: list[str] | None = None) -> list[Suggestion]:
        """APPLIED rows of a run, optionally narrowed to sharedIds."""
        ...

    @abstractmethod
    def query(self, run_id: str, f: SuggestionFilter) -> tuple[list[Suggestion], int]:
        """Return ``(page rows, total matching)`` for the filter's window.

        ``total`` is the count over the WHOLE filter (ignoring limit/offset), so
        the grid can show "showing 25 of 4,318" without a second query.
        """
        ...

    @abstractmethod
    def entity_rows(self, run_id: str, f: SuggestionFilter) -> tuple[list[EntityReviewRow], int]:
        """Return (page of per-entity aggregate rows, total distinct entities matching f).

        ``total`` is the distinct-shared_id count over the WHOLE filter (limit/offset
        ignored for the count), so the grid shows "N entities". Ordering: MIN(suggestion.id).

        Legacy grouping: one row per (entity, LANGUAGE). The review table now reads
        :meth:`operations` (one row per entity); this stays for the callers that
        still speak entity-language rows.
        """
        ...

    @abstractmethod
    def operations(self, run_id: str, f: SuggestionFilter) -> tuple[list[OperationReviewRow], int]:
        """Return (page of per-ENTITY operation rows, total distinct entities matching f).

        The review table's read: one row per affected entity, whatever languages
        it writes, with that page's SUBOPERATION rows attached. The filter is a
        suboperation filter (``property_name``/``match``/``op``/``status``/
        ``languages`` narrow the parts), so an operation with no matching part is
        absent from both the page and ``total``. ``total`` ignores limit/offset.
        """
        ...

    @abstractmethod
    def property_scores(self, run_id: str) -> dict[str, tuple[int, int]]:
        """``property -> (exact, mismatch)`` verdict counts for the whole run (the Property filter)."""
        ...

    @abstractmethod
    def languages(self, run_id: str) -> list[str]:
        """The distinct written languages of the run — the Language filter's options."""
        ...

    @abstractmethod
    def templates(self, run_id: str) -> list[str]:
        """The distinct template names of the run — used to resolve property labels."""
        ...

    @abstractmethod
    def selection_rows(self, run_id: str, f: SuggestionFilter) -> list[tuple[int, str, str | None, str]]:
        """Every matching row's ``(id, sharedId, language, op)`` — the selection projection.

        The whole-result selection ("select all results") spans every page, so the
        bulk actions resolve their identities from the store through this
        projection. Limit/offset are ignored on purpose.
        """
        ...

    @abstractmethod
    def get_entity(self, run_id: str, shared_id: str, language: str | None = None) -> list[Suggestion]:
        """Every suggestion row for one entity in ONE language group (the entity window).

        ``language=None`` is the language-independent group (selects, titles,
        whole-entity ops), not "every language" — the table's rows are per
        language, so the window reads exactly the group it was opened for.
        """
        ...

    @abstractmethod
    def entity_languages(self, run_id: str, f: SuggestionFilter) -> list[tuple[str, str | None]]:
        """Every (sharedId, language) pair matching a filter, first-appearance order."""
        ...

    @abstractmethod
    def set_status(
        self,
        ids: list[int],
        status: SuggestionStatus,
        edited_value: str | None = None,
        reason: str | None = None,
    ) -> None:
        """Set the review status of explicit rows (per-row buttons, selection bulk)."""
        ...

    @abstractmethod
    def set_status_by_filter(
        self,
        run_id: str,
        f: SuggestionFilter,
        status: SuggestionStatus,
        reason: str | None = None,
    ) -> int:
        """Set the status of every row matching the CURRENT filter; return the count.

        The filter-scoped bulk status path ("Accept all" over a filter): the count returned is
        what the confirm dialog showed, and the update is a single SQL statement
        rather than a Python loop over ids.
        """
        ...

    @abstractmethod
    def reset_statuses(self, run_id: str, ids: list[int]) -> None:
        """Return DISCARDED rows to PENDING ("Undo discard").

        Only the discard state is undoable this way: ACCEPTED/EDITED/APPLIED rows
        carry operator intent or have already been written, so they keep their
        status.
        """
        ...

    @abstractmethod
    def discard_by_filter(
        self,
        run_id: str,
        f: SuggestionFilter,
        reason: str | None = None,
    ) -> int:
        """Mark every row matching the filter DISCARDED; return how many moved.

        The discard is the operator's "do NOT execute this": the review table's
        per-operation Discard action and its bulk Discard over the selection both
        land here, with the reason the operator typed.

        The rows ALREADY WRITTEN (APPLIED) keep their status: a discard says
        what must not be executed, and un-writing what Uwazi already holds is
        the REVERT's job. Flipping an applied row to discarded would also hide
        it from the run's revert, which finds its rows by that status.
        """
        ...

    @abstractmethod
    def restore_by_filter(self, run_id: str, f: SuggestionFilter) -> int:
        """Return the filter's DISCARDED rows to PENDING (undo a discard); return the count.

        The counterpart of :meth:`discard_by_filter`: an operation the operator
        marked as discarded can be brought back, and its rows become runnable
        again. Rows in any other status are untouched.
        """
        ...

    @abstractmethod
    def property_names(self, run_id: str) -> list[str]:
        """Distinct property names present in the set (the property dropdown)."""
        ...

    @abstractmethod
    def shared_ids(self, run_id: str, f: SuggestionFilter) -> list[str]:
        """The distinct sharedIds matching the filter (subset re-predict/apply).

        Ignores limit/offset on purpose: this is a projection of the selection,
        not a page of it.
        """
        ...

    @abstractmethod
    def counts(self, run_id: str) -> dict[str, int]:
        """Aggregate counters for the header cards, computed in the store.

        The status keys count ENTITY-LANGUAGE GROUPS — the rows the review table
        itself shows — so the cards and the table always agree:
        ``total`` is the distinct (sharedId, language) count and each of
        ``pending``/``accepted``/``edited``/``discarded``/``applied``/``reverted``
        counts the groups carrying at least one row in that status (a group whose
        rows span statuses is counted in each). ``match_exact``/``match_mismatch``/
        ``match_new`` stay row-level: they are per-property comparison verdicts.

        Grouped SQL — never a Python pass over 10k rows.

        The ``subs_*`` keys are ROW-level (the table's suboperations — one
        proposal per property/language/link) and are what the header cards read:
        ``subs_total`` plus one count per status. ``operations_total`` is the
        table's own row count (distinct sharedId).
        """
        ...

    @abstractmethod
    def mark_applied_reverted(self, run_id: str) -> int:
        """Mark every APPLIED row of ``run_id`` REVERTED; return how many moved.

        Called after a successful run-level revert: those writes no longer
        exist, so the rows must read as reverted (runnable again) rather than
        applied. Scoped to the run, since one revert
        undoes the run's whole accumulated manifest.
        """
        ...

    @abstractmethod
    def mark_applied_reverted_for(self, run_id: str, shared_ids: list[str]) -> int:
        """Mark APPLIED rows REVERTED for exactly these sharedIds (any language); return the count.

        Called after a per-entity (per-row) revert: one restore rolled the WHOLE
        entity back — every language row's write — so all the entity's APPLIED
        rows move, across languages.
        """
        ...

    @abstractmethod
    def delete_run(self, run_id: str) -> None:
        """Remove every suggestion + pass row for a run (run deletion flow)."""
        ...

    @abstractmethod
    def rename_run(self, old_id: str, new_id: str) -> None:
        """Re-key every row of ``old_id`` to ``new_id`` (the run-rename flow).

        The run's review table, its prediction pass and its drift rows are
        all keyed by ``run_id``; the backup store renames the run FOLDER, so
        without this the review page of the renamed run opens empty and the old
        name keeps orphaned rows. Idempotent when no rows exist for ``old_id``.
        """
        ...


__all__ = ["SuggestionStorePort"]
