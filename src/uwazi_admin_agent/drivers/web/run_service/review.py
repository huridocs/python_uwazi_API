"""Web driver module split out of the original ``run_service`` (behavior preserved)."""

from __future__ import annotations

from loguru import logger

from uwazi_admin_agent.domain.select_values import ALL_VALUES_REJECTED_REASON, all_values_rejected
from uwazi_admin_agent.domain.suggestion import (
    EntityReviewRow,
    OperationReviewRow,
    PredictionPass,
    Suggestion,
    SuggestionFilter,
    SuggestionStatus,
)
from uwazi_admin_agent.drivers.runtime import build_runtime

from .wiring import build_suggestion_store

_PROPERTY_LABELS: dict[tuple[str, str, str], dict[str, str]] = {}


_PROPERTY_TYPES: dict[tuple[str, str, str], dict[str, str]] = {}


_SELECT_CATALOG: dict[tuple[str, str, str, str], dict[str, str]] = {}


def get_suggestions(url: str, run_id: str, f: SuggestionFilter) -> tuple[list[Suggestion], int]:
    """Return a page of suggestions + the filter's total (the review grid's read)."""
    return build_suggestion_store(url).query(run_id, f)


def current_pass(url: str, run_id: str) -> PredictionPass | None:
    """The run's single prediction pass row, or None when none is recorded.

    The review page never carries a pass number — the table is ONE live table
    per run — so the review reads below resolve the current pass HERE. A later
    pass overwrites this row, and that is the one the review surface follows.
    """
    return build_suggestion_store(url).get_pass(run_id)


def get_suggestion_entity_languages(url: str, run_id: str, f: SuggestionFilter) -> list[tuple[str, str | None]]:
    """Return every (sharedId, language) row matching a filter (the window's prev/next)."""
    return build_suggestion_store(url).entity_languages(run_id, f)


def get_suggestion_counts(url: str, run_id: str) -> dict[str, int]:
    """Return the review header's aggregate counters (SQL-side group-bys).

    The status keys count ENTITY-LANGUAGE GROUPS — the rows the review table shows
    — so the cards and the table agree; ``match_*`` stay row-level.
    """
    return build_suggestion_store(url).counts(run_id)


def get_suggestion_properties(url: str, run_id: str) -> list[str]:
    """Return the distinct property names in a set (the property dropdown)."""
    return build_suggestion_store(url).property_names(run_id)


def get_entity_suggestions(
    url: str,
    run_id: str,
    shared_id: str,
    language: str | None = None,
    f: SuggestionFilter | None = None,
) -> list[Suggestion]:
    """Return the suggestions of one entity's language group (the entity window's read).

    ``f`` is the review table's current filter, so the detail window shows the
    same properties the table does; ``language=None`` reads the
    language-independent group, exactly as the table's own rows do.
    """
    return build_suggestion_store(url).get_entity(run_id, shared_id, language, f)


def property_labels_for(url: str, template_name: str | None, user: str, password: str) -> dict[str, str]:
    """``{property_name: label}`` for one template, cached; ``{}`` when unresolvable.

    A suggestion row names its property by the template's property ``name``
    (``document_ref``), which reads as an identifier on screen; Uwazi shows the
    property's ``label`` ("Document Ref"), and so does the entity window. The
    lookup goes through the template-label port (one cached ``/api/templates``
    read behind it) and degrades to ``{}`` — the caller keeps showing the raw
    names — whenever the instance cannot be reached or the template is unknown, so
    a display path never depends on it.
    """
    if not template_name:
        return {}
    key = (url, user, template_name)
    cached = _PROPERTY_LABELS.get(key)
    if cached is not None:
        return cached
    labels: dict[str, str] = {}
    try:
        runtime = build_runtime(user=user, password=password, url=url)
        labels = dict(runtime.template_labels.property_labels(template_name)) if runtime.template_labels else {}
    except Exception as exc:  # noqa: BLE001 — a display lookup never blocks the window
        logger.warning("could not resolve property labels for template {}: {}", template_name, exc)
    _PROPERTY_LABELS[key] = labels
    return labels


def property_types_for(url: str, template_name: str | None, user: str, password: str) -> dict[str, str]:
    """``{property_name: type}`` for one template, cached; ``{}`` when unresolvable.

    The review table and the entity window need the property TYPE to render a
    value correctly: a ``date``/``daterange``/``multidate`` family value is
    stored as a numeric timestamp and would otherwise print as a raw epoch
    number. The lookup goes through the property-type port (the same cached
    ``/api/templates`` read the label lookup uses) and degrades to ``{}`` so a
    display path never depends on the instance being reachable.
    """
    if not template_name:
        return {}
    key = (url, user, template_name)
    cached = _PROPERTY_TYPES.get(key)
    if cached is not None:
        return cached
    types: dict[str, str] = {}
    try:
        runtime = build_runtime(user=user, password=password, url=url)
        types = dict(runtime.property_types.property_types(template_name)) if runtime.property_types else {}
    except Exception as exc:  # noqa: BLE001 — a display lookup never blocks the window
        logger.warning("could not resolve property types for template {}: {}", template_name, exc)
    _PROPERTY_TYPES[key] = types
    return types


def set_selected_values(url: str, run_id: str, row_id: int, selected: list[str] | None) -> Suggestion:
    """Persist one multiselect row's explicit selection and its derived status; return the fresh row.

    ``selected`` is the FULL set of labels the Run writes (not a delta); ``None``
    drops the explicit selection so the proposal minus ``rejected_values``
    applies again. The same guards as :func:`set_rejected_values` hold: APPLIED
    rows raise "already applied — revert it first", OBSOLETE rows "refresh the
    table", and a row discarded for another reason "restore it first". An empty
    list means "nothing selected": the row becomes DISCARDED with
    ``ALL_VALUES_REJECTED_REASON`` (a Run writes nothing); any non-empty
    selection — or clearing it — puts a row discarded by that sentinel back
    into PENDING with the reason cleared.
    """
    store = build_suggestion_store(url)
    found, _total = store.query(
        run_id,
        SuggestionFilter(suggestion_ids=[row_id], include_obsolete=True, limit=1, offset=0),
    )
    if not found:
        raise ValueError(f"row {row_id} not found in run {run_id}")
    row = found[0]
    if row.status is SuggestionStatus.APPLIED:
        raise ValueError("already applied — revert it first")
    if row.status is SuggestionStatus.OBSOLETE:
        raise ValueError("refresh the table")
    if row.discard_reason not in (None, ALL_VALUES_REJECTED_REASON):
        raise ValueError("restore it first")
    sentinel_discarded = row.status is SuggestionStatus.DISCARDED and row.discard_reason == ALL_VALUES_REJECTED_REASON
    status: SuggestionStatus | None = None
    reason: str | None = None
    if selected is not None and not selected:
        status = SuggestionStatus.DISCARDED
        reason = ALL_VALUES_REJECTED_REASON
    elif sentinel_discarded:
        status = SuggestionStatus.PENDING
        reason = None
    store.set_selected_values(row_id, selected, status, reason)
    fresh, _total = store.query(
        run_id,
        SuggestionFilter(suggestion_ids=[row_id], include_obsolete=True, limit=1, offset=0),
    )
    if not fresh:
        raise ValueError(f"row {row_id} not found in run {run_id}")
    return fresh[0]


def select_value_catalog(
    url: str,
    template_name: str | None,
    property_name: str | None,
    user: str,
    password: str,
) -> dict[str, str]:
    """``{label: value id}`` for one select/multiselect property, cached; ``{}`` when unresolvable.

    The entity window's value picker lists every thesaurus value of the property,
    not just the ones the script proposed, so the operator can add one. The map
    is returned whole (not a label list) because a grouped thesaurus maps BOTH
    ``"Child"`` and ``"Group: Child"`` to one id: the caller collapses those by
    id. Degrades to ``{}`` whenever the instance cannot be reached or the
    property carries no thesaurus — a display lookup never blocks the window.
    """
    if not template_name or not property_name:
        return {}
    key = (url, user, template_name, property_name)
    cached = _SELECT_CATALOG.get(key)
    if cached is not None:
        return cached
    ids: dict[str, str] = {}
    try:
        runtime = build_runtime(user=user, password=password, url=url)
        ids = dict(runtime.thesaurus_values.value_ids(template_name, property_name)) if runtime.thesaurus_values else {}
    except Exception as exc:  # noqa: BLE001 — a display lookup never blocks the window
        logger.warning(
            "could not resolve thesaurus values for {}.{}: {}",
            template_name,
            property_name,
            exc,
        )
    _SELECT_CATALOG[key] = ids
    return ids


def get_entity_rows(url: str, run_id: str, f: SuggestionFilter) -> tuple[list[EntityReviewRow], int]:
    """Return a page of per-entity aggregate rows + the distinct-entity total.

    The entity table's read: one row per affected entity (checkbox column feeds
    the selection-driven actions), grouped in SQL like every other review read.
    """
    return build_suggestion_store(url).entity_rows(run_id, f)


def get_operation_rows(url: str, run_id: str, f: SuggestionFilter) -> tuple[list[OperationReviewRow], int]:
    """Return a page of OPERATIONS (one row per entity) with their suboperations.

    The review table's read: the filter is a SUBOPERATION filter, so an operation
    whose every part the filter removed never reaches the page. ``subs`` carries
    the page's matching suggestion rows.
    """
    return build_suggestion_store(url).operations(run_id, f)


def get_property_scores(url: str, run_id: str) -> dict[str, tuple[int, int]]:
    """``property -> (exact, mismatch)`` over the whole run (the Property filter)."""
    return build_suggestion_store(url).property_scores(run_id)


def get_templates(url: str, run_id: str) -> list[str]:
    """The distinct template names of the run — the property-label lookup."""
    return build_suggestion_store(url).templates(run_id)


def get_suggestion_languages(url: str, run_id: str) -> list[str]:
    """The distinct languages the run writes — the Language filter's options."""
    return build_suggestion_store(url).languages(run_id)


def get_op_status_counts(url: str, run_id: str) -> dict[str, dict[str, int]]:
    """``op``/``status`` row counts over the whole run — the filter option labels."""
    return build_suggestion_store(url).op_status_counts(run_id)


def get_selection_rows(
    url: str,
    run_id: str,
    f: SuggestionFilter,
    *,
    runnable_only: bool = False,
) -> list[tuple[int, str, str | None, str]]:
    """Every matching row's ``(id, sharedId, language, op)`` — the whole-result selection.

    ``runnable_only`` restricts the projection to the statuses a Run may write
    (PENDING/ACCEPTED/EDITED/REVERTED). "Select all" uses it so already-applied
    or discarded parts cannot inflate the Run/Discard counts; entity navigation
    reads the whole projection so it can still step through every row.
    """
    return build_suggestion_store(url).selection_rows(run_id, f, runnable_only=runnable_only)


def discard_rows(url: str, run_id: str, pairs: list[tuple[str, str | None]], reason: str | None = None) -> int:
    """Mark the given entity-language groups' rows DISCARDED; return how many moved.

    The review table's Discard action, per row and over the checked selection:
    "the end user does not want this executed". The rows are marked, not
    deleted — a Run skips a discarded row (it accepts only PENDING/REVERTED
    ones), and the operator can bring the operation back with the Undo button.

    The groups are addressed as ``(sharedId, language)`` pairs, the table's own
    row identity, so discarding a translation's ``es`` row leaves its ``en``
    sibling alone. An empty list discards nothing.
    """
    store = build_suggestion_store(url)
    return store.discard_by_filter(
        run_id,
        SuggestionFilter(entity_languages=pairs),
        reason,
    )


def restore_rows(url: str, run_id: str, pairs: list[tuple[str, str | None]]) -> int:
    """Undo a discard: the groups' DISCARDED rows go back to PENDING; return the count.

    The counterpart of :func:`discard_rows` — an operation the operator marked
    as discarded becomes runnable again.
    """
    store = build_suggestion_store(url)
    return store.restore_by_filter(
        run_id,
        SuggestionFilter(entity_languages=pairs),
    )


def discard_ids(url: str, run_id: str, ids: list[int], reason: str | None = None) -> int:
    """Mark exactly these SUBOPERATIONS discarded; return how many moved.

    The table's Discard acts on checked suboperation rows, so the selection is a
    suggestion-id list — narrowing a discard to one property of one
    entity-language group, which the pair-based :func:`discard_rows` cannot
    express. APPLIED rows are left alone (as in every discard path: un-writing
    is the revert's job). An empty list discards nothing.
    """
    if not ids:
        return 0
    store = build_suggestion_store(url)
    return store.discard_by_filter(
        run_id,
        SuggestionFilter(suggestion_ids=ids),
        reason,
    )


def restore_ids(url: str, run_id: str, ids: list[int]) -> int:
    """Undo the discard of exactly these SUBOPERATIONS; return the count."""
    if not ids:
        return 0
    store = build_suggestion_store(url)
    return store.restore_by_filter(
        run_id,
        SuggestionFilter(suggestion_ids=ids),
    )


def set_rejected_values(url: str, run_id: str, row_id: int, rejected: list[str]) -> Suggestion:
    """Persist one multiselect row's rejected labels and its derived status; return the fresh row.

    Rules: only runnable rows and rows discarded BY this mechanism
    (``discard_reason == ALL_VALUES_REJECTED_REASON``) may be edited; APPLIED
    rows raise ``ValueError`` "already applied — revert it first", OBSOLETE rows
    "refresh the table", and a row discarded for another reason raises
    ``ValueError`` "restore it first". When
    ``all_values_rejected(proposal, rejected)`` the row becomes DISCARDED with
    reason ``ALL_VALUES_REJECTED_REASON`` (a Run then writes nothing); when
    values are accepted again and the row's discard reason is that same
    sentinel, it returns to PENDING with the reason cleared. Any other discard
    reason is never touched.
    """
    store = build_suggestion_store(url)
    found, _total = store.query(
        run_id,
        SuggestionFilter(suggestion_ids=[row_id], include_obsolete=True, limit=1, offset=0),
    )
    if not found:
        raise ValueError(f"row {row_id} not found in run {run_id}")
    row = found[0]
    if row.status is SuggestionStatus.APPLIED:
        raise ValueError("already applied — revert it first")
    if row.status is SuggestionStatus.OBSOLETE:
        raise ValueError("refresh the table")
    if row.discard_reason not in (None, ALL_VALUES_REJECTED_REASON):
        raise ValueError("restore it first")
    status: SuggestionStatus | None = None
    reason: str | None = None
    if all_values_rejected(row.proposed_value, rejected):
        status = SuggestionStatus.DISCARDED
        reason = ALL_VALUES_REJECTED_REASON
    elif row.status is SuggestionStatus.DISCARDED and row.discard_reason == ALL_VALUES_REJECTED_REASON:
        status = SuggestionStatus.PENDING
        reason = None
    store.set_rejected_values(row_id, rejected, status, reason)
    fresh, _total = store.query(
        run_id,
        SuggestionFilter(suggestion_ids=[row_id], include_obsolete=True, limit=1, offset=0),
    )
    if not fresh:
        raise ValueError(f"row {row_id} not found in run {run_id}")
    return fresh[0]
