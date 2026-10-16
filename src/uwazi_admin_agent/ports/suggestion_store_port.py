"""Persistence + query seam for prediction attempts and their suggestions.

The review UI's 10k-entity scale requirement lives here: every selection surface
(the grid's checkboxes, the bulk accept/discard buttons, the subset re-predict,
the subset apply) resolves to either an explicit id list or a
:class:`SuggestionFilter`, and this port's implementations do the filtering,
paginating, and aggregating in the store — never by loading every row into the
UI.

Two record families:

- :class:`PredictionAttempt` — one row per pass, polled while it runs (progress)
  and read afterwards (spent vs budget, abort reason).
- :class:`Suggestion` — the reviewable rows, upserted idempotently on
  ``(run_id, attempt, shared_id, op, property_name)`` so re-running a pass over
  the same scope overwrites those rows instead of duplicating them.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from uwazi_admin_agent.domain.suggestion import (
    EntityReviewRow,
    PredictionAttempt,
    Suggestion,
    SuggestionFilter,
    SuggestionStatus,
)


class SuggestionStorePort(ABC):
    """Storage + query API for prediction attempts and suggestions."""

    @abstractmethod
    def ensure_schema(self) -> None:
        """Create the tables/indexes when missing (idempotent; safe on every build)."""
        ...

    # --- attempts ------------------------------------------------------------

    @abstractmethod
    def create_attempt(self, attempt: PredictionAttempt) -> None:
        """Insert a pass row (the UI starts polling it for progress)."""
        ...

    @abstractmethod
    def update_attempt(self, attempt: PredictionAttempt) -> None:
        """Overwrite a pass row (progress ticks and terminal transitions)."""
        ...

    @abstractmethod
    def get_attempt(self, run_id: str, attempt: int) -> PredictionAttempt | None:
        """One pass row, or None."""
        ...

    @abstractmethod
    def latest_attempt(self, run_id: str) -> PredictionAttempt | None:
        """The highest-numbered pass row for a run, or None."""
        ...

    @abstractmethod
    def list_attempts(self, run_id: str) -> list[PredictionAttempt]:
        """Every pass row for a run, newest first (the attempt dropdown)."""
        ...

    # --- suggestions ---------------------------------------------------------

    @abstractmethod
    def add_suggestions(self, suggestions: list[Suggestion]) -> None:
        """Upsert suggestion rows (idempotent on run/attempt/shared_id/op/property).

        An upsert, not an insert: a re-run of the same pass over the same scope
        must overwrite those rows. Operator review state (status/edited value)
        is NOT clobbered by an upsert of the same key — the row is a prediction
        identity, so its proposal fields refresh while its review fields survive.
        """
        ...

    @abstractmethod
    def query(self, run_id: str, attempt: int, f: SuggestionFilter) -> tuple[list[Suggestion], int]:
        """Return ``(page rows, total matching)`` for the filter's window.

        ``total`` is the count over the WHOLE filter (ignoring limit/offset), so
        the grid can show "showing 25 of 4,318" without a second query.
        """
        ...

    @abstractmethod
    def entity_rows(self, run_id: str, attempt: int, f: SuggestionFilter) -> tuple[list[EntityReviewRow], int]:
        """Return (page of per-entity aggregate rows, total distinct entities matching f).

        ``total`` is the distinct-shared_id count over the WHOLE filter (limit/offset
        ignored for the count), so the grid shows "N entities". Ordering: MIN(suggestion.id).
        """
        ...

    @abstractmethod
    def get_entity(self, run_id: str, attempt: int, shared_id: str, language: str | None = None) -> list[Suggestion]:
        """Every suggestion row for one entity in ONE language group (the entity window).

        ``language=None`` is the language-independent group (selects, titles,
        whole-entity ops), not "every language" — the table's rows are per
        language, so the window reads exactly the group it was opened for.
        """
        ...

    @abstractmethod
    def entity_languages(self, run_id: str, attempt: int, f: SuggestionFilter) -> list[tuple[str, str | None]]:
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
        attempt: int,
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
    def reset_statuses(self, run_id: str, attempt: int, ids: list[int]) -> None:
        """Return DISCARDED rows to PENDING ("Undo discard").

        Only the discard state is undoable this way: ACCEPTED/EDITED/APPLIED rows
        carry operator intent or have already been written, so they keep their
        status.
        """
        ...

    @abstractmethod
    def property_names(self, run_id: str, attempt: int) -> list[str]:
        """Distinct property names present in the set (the property dropdown)."""
        ...

    @abstractmethod
    def shared_ids(self, run_id: str, attempt: int, f: SuggestionFilter) -> list[str]:
        """The distinct sharedIds matching the filter (subset re-predict/apply).

        Ignores limit/offset on purpose: this is a projection of the selection,
        not a page of it.
        """
        ...

    @abstractmethod
    def counts(self, run_id: str, attempt: int) -> dict[str, int]:
        """Aggregate counters for the header cards, computed in the store.

        The status keys count ENTITY-LANGUAGE GROUPS — the rows the review table
        itself shows — so the cards and the table always agree:
        ``total`` is the distinct (sharedId, language) count and each of
        ``pending``/``accepted``/``edited``/``discarded``/``applied``/``reverted``
        counts the groups carrying at least one row in that status (a group whose
        rows span statuses is counted in each). ``match_exact``/``match_mismatch``/
        ``match_new`` stay row-level: they are per-property comparison verdicts.

        Grouped SQL — never a Python pass over 10k rows.
        """
        ...

    @abstractmethod
    def mark_applied_reverted(self, run_id: str) -> int:
        """Mark every APPLIED row of ``run_id`` REVERTED; return how many moved.

        Called after a successful revert: those writes no longer exist, so the
        rows must read as reverted (runnable again) rather than applied. Scoped
        to the run, across all its attempts, since one revert undoes the run's
        whole accumulated manifest.
        """
        ...

    @abstractmethod
    def delete_run(self, run_id: str) -> None:
        """Remove every suggestion + attempt row for a run (run deletion flow)."""
        ...


__all__ = ["SuggestionStorePort"]
