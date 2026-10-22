"""Prediction suggestions + the review filters that select them (pure models).

A **prediction pass** is a dry run whose recorded would-be writes are persisted
as :class:`Suggestion` rows. The review UI then lets the operator accept, edit,
or discard each suggestion before an *apply* run pushes the accepted property
values to Uwazi. Everything here is a value object (frozen where it is an
immutable record) plus the filter object the store compiles into SQL — the
10k-entity scale guarantee depends on selection living in the filter, never in
Python-side iteration.

Op coverage matches the dry-run recorder exactly (``_dry_run_write_helpers``):
``update`` / ``create`` records explode into one suggestion per metadata key
(plus ``_title`` when the record carries a title), while whole-entity ops
(``delete`` / ``publish`` / ``unpublish`` / ``set_publish_status`` /
``create_relationships``) produce one row each.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from uwazi_admin_agent.domain.llm_budget import LlmBudget, LlmUsage

# The ops a recorded would-be write can carry. Mirrors the dry-run recorder's
# labels verbatim (no translation layer between the recorder and the review UI),
# plus the two MERGE ops the conversion derives when a script's update/create
# record is paired with the source deletes of a file move.
SuggestionOp = Literal[
    "update",
    "create",
    "delete",
    "publish",
    "unpublish",
    "set_publish_status",
    "create_relationships",
    "update_and_delete",
    "create_and_delete",
]

# Ops whose table rows carry a proposed property value (and so get current-value
# hydration, inline editing, and the update/create apply payloads).
UPDATE_OPS: frozenset[str] = frozenset({"update", "update_and_delete"})
CREATE_OPS: frozenset[str] = frozenset({"create", "create_and_delete"})
PROPERTY_OPS: frozenset[str] = UPDATE_OPS | CREATE_OPS
# A merge is ONE review row that writes a target (new or existing) and removes
# every source in the same Run; the sources are never separate table rows.
MERGE_OPS: frozenset[str] = frozenset({"update_and_delete", "create_and_delete"})

# Synthetic property name used for a whole-entity title change on an
# update/create record (metadata keys use their own name).
TITLE_PROPERTY: str = "_title"


class SuggestionStatus(str, Enum):
    """Review lifecycle of one suggestion."""

    PENDING = "pending"
    ACCEPTED = "accepted"
    EDITED = "edited"
    DISCARDED = "discarded"
    APPLIED = "applied"
    REVERTED = "reverted"
    OBSOLETE = "obsolete"


class PredictionAttemptStatus(str, Enum):
    """Lifecycle of one prediction pass over a script."""

    RUNNING = "running"
    COMPLETED = "completed"
    ABORTED = "aborted"
    FAILED = "failed"


class Suggestion(BaseModel):
    """One proposed change to one entity property, as the review UI presents it.

    ``shared_id`` / ``op`` / ``property_name`` identify the row (the store's
    idempotency key adds ``run_id`` + ``attempt``). ``proposed_value`` is what
    the script would write; ``current_value`` is Uwazi's value at prediction
    time (fetched during persistence, since dry-run records carry no
    before-state); ``match`` says whether the two agree, with ``None`` meaning
    "no current value to compare against" (a new value, not a mismatch) or "not
    a property update" (whole-entity ops).
    """

    model_config = ConfigDict(frozen=True)

    id: int | None = Field(
        default=None,
        description=(
            "The store's row id, assigned on persistence and read back by queries. "
            "None for a freshly built suggestion (before the store assigns one). "
            "The per-row actions (Accept/Discard/Edit) address rows by this id."
        ),
    )
    run_id: str = Field(description="The run this prediction belongs to.")
    attempt: int = Field(description="The prediction attempt number (1-based, per run).")
    shared_id: str = Field(description="The entity's Uwazi sharedId (placeholder for creates).")
    entity_title: str = Field(default="", description="The entity title for display, when known.")
    template_name: str | None = Field(
        default=None,
        description="The entity's template name when the script recorded it (the review table's Template column).",
    )
    op: SuggestionOp = Field(description="Which recorded op produced this suggestion.")
    language: str | None = Field(
        default=None,
        description=(
            "The language this row's value is written in, when the property is a per-language one "
            "(text, richtext): Uwazi stores those as [{'value', 'language'}] lists, and a translation "
            "task records one row PER LANGUAGE. None means the value is language-independent — a "
            "select/multiselect/link/date value, a title, or a whole-entity op — which is also how a "
            "multi-language value that was never split reads (see records_to_suggestions)."
        ),
    )
    property_name: str | None = Field(
        default=None,
        description=(
            "The metadata key this row proposes (or `_title` for a title change); "
            "None for whole-entity ops (delete/publish/...)."
        ),
    )
    merge_source_ids: list[str] = Field(
        default_factory=list,
        description=(
            "For the two merge ops (update_and_delete/create_and_delete): the source "
            "entities the same Run deletes after writing the target. Empty for every "
            "other op. The sources are part of THIS row's action, not separate rows."
        ),
    )
    detail_key: str | None = Field(
        default=None,
        description=(
            "A per-row discriminator INSIDE one (sharedId, op, language, property) identity, "
            "for ops whose several rows share all of those: a `create_relationships` batch "
            "records one row per link, and every link of one entity has the same sharedId, "
            "op, language and (null) property name — so the link's target sharedId rides "
            "here. None for every other op (and for legacy rows), which keeps their identity "
            "exactly what it was."
        ),
    )
    proposed_value: Any = Field(default=None, description="The value the script would write (raw metadata shape).")
    current_value: Any = Field(default=None, description="Uwazi's current value for that key, when fetched.")
    match: bool | None = Field(
        default=None,
        description=(
            "True when proposed equals current, False when it differs, None when there is "
            "no current value (new value) or the op carries no property comparison."
        ),
    )
    status: SuggestionStatus = Field(default=SuggestionStatus.PENDING, description="Review state.")
    edited_value: str | None = Field(default=None, description="Operator-supplied replacement (status=EDITED).")
    discard_reason: str | None = Field(default=None, description="Why the operator discarded this row.")
    reviewed_at: datetime | None = Field(default=None, description="When the operator last changed the status.")
    created_at: datetime | None = Field(default=None, description="When the suggestion was persisted.")


class EntityReviewRow(BaseModel):
    """One affected entity IN ONE LANGUAGE, aggregated from its suggestions.

    The review table is one row per affected entity (the checkbox feeds bulk
    accept / discard / generate-outcomes), not one row per suggestion: a
    suggestion set of thousands groups into a table of distinct entities.
    ``ops`` / ``properties`` follow first-appearance order so the display
    mirrors the script's own order; the counters are SQL ``SUM``s over the
    group's rows.

    ``language`` is part of the grouping key: a translation task updates the
    same entity in several languages and the operator reviews each of them
    separately, so the same sharedId can appear once per language. ``None`` is
    the language-independent group (a select change, a title, a delete) and is
    kept as its own row rather than repeated into every language group.
    """

    model_config = ConfigDict(frozen=True)

    shared_id: str = Field(description="The entity's sharedId (a create row keeps its dry-created-N placeholder).")
    language: str | None = Field(
        default=None,
        description="The language this row is written in; None = language-independent (the '—' group).",
    )
    entity_title: str = Field(default="", description="Display title, when any row recorded one.")
    template_names: list[str] = Field(
        default_factory=list,
        description="Distinct template names among the entity's rows, first-appearance order.",
    )
    ops: list[str] = Field(description="Distinct ops among the entity's rows, first-appearance order.")
    properties: list[str] = Field(description="Distinct non-null property names, first-appearance order.")
    proposal_count: int = Field(ge=0, description="Total suggestion rows for the entity.")
    pending: int = Field(ge=0, description="Rows still in review.")
    accepted: int = Field(ge=0, description="Rows accepted (not yet applied).")
    applied: int = Field(ge=0, description="Rows already written to Uwazi.")
    reverted: int = Field(ge=0, description="Written rows whose writes a revert undid (runnable again).")
    discarded: int = Field(ge=0, description="Rows rejected/discarded.")
    match_summary: str = Field(
        description=(
            "One of 'exact' | 'mismatch' | 'new' | 'mixed' | 'n/a': the single match verdict of the "
            "entity's property rows, 'mixed' when verdicts disagree, 'n/a' when none carries one."
        ),
    )
    has_outcome: bool = Field(
        description=(
            "True when at least one update/create row carries a proposed value — i.e. the "
            "'Generate outcomes' pass has filled the stored value this entity would receive."
        ),
    )


class OperationReviewRow(BaseModel):
    """One affected ENTITY — the review table's operation — and its suboperations.

    The table's row is the entity, so its key is the ``shared_id`` alone: a
    translation task that writes the same entity in ``es`` and ``en`` is ONE
    operation, and each language's property rows are SUBOPERATIONS of it (see
    ``Suggestion``). ``subs`` carries those rows, already narrowed by the page's
    filter — a filter is a SUBOPERATION filter, so an operation whose every part
    the filter removed never reaches the table in the first place.

    The counters are SQL sums over the SAME filter, and ``languages`` is the
    distinct language set of the matching rows (the operation has no language of
    its own; the language is a suboperation attribute).
    """

    model_config = ConfigDict(frozen=True)

    shared_id: str = Field(description="The entity's sharedId (a create row keeps its dry-created-N placeholder).")
    entity_title: str = Field(default="", description="Display title, when any row recorded one.")
    template_names: list[str] = Field(
        default_factory=list,
        description="Distinct template names among the entity's rows, first-appearance order.",
    )
    ops: list[str] = Field(description="Distinct ops among the entity's rows, first-appearance order.")
    properties: list[str] = Field(description="Distinct non-null property names, first-appearance order.")
    languages: list[str] = Field(
        default_factory=list,
        description=(
            "Distinct languages among the matching rows (sorted, language-independent rows excluded — "
            "they carry no language). Display/navigation only; the table shows language per suboperation."
        ),
    )
    proposal_count: int = Field(ge=0, description="Total matching suggestion rows for the entity.")
    pending: int = Field(ge=0, description="Rows still in review.")
    accepted: int = Field(ge=0, description="Rows accepted (not yet applied).")
    applied: int = Field(ge=0, description="Rows already written to Uwazi.")
    reverted: int = Field(ge=0, description="Written rows whose writes a revert undid (runnable again).")
    discarded: int = Field(ge=0, description="Rows rejected/discarded.")
    edited: int = Field(ge=0, description="Rows carrying an operator edit.")
    match_summary: str = Field(
        description=(
            "One of 'exact' | 'mismatch' | 'new' | 'mixed' | 'n/a': the single match verdict of the "
            "entity's property rows, 'mixed' when verdicts disagree, 'n/a' when none carries one."
        ),
    )
    has_outcome: bool = Field(
        description=(
            "True when at least one update/create row carries a proposed value — i.e. the "
            "'Generate outcomes' pass has filled the stored value this entity would receive."
        ),
    )
    subs: list[Suggestion] = Field(
        default_factory=list,
        description=(
            "The operation's suboperations — one Suggestion row each, in first-appearance order, "
            "already narrowed by the page's filter. Loaded per page, never for the whole set."
        ),
    )


class SuggestionFilter(BaseModel):
    """The selection predicate every review surface produces.

    The grid's checkboxes, the bulk accept/discard buttons, the subset
    re-predict, and the subset apply all funnel into either this object (a
    server-side query) or an explicit id list — never a Python-side pass over
    loaded rows. ``limit``/``offset`` are the pagination window; ``counts`` and
    ``shared_ids`` ignore them (they aggregate / project instead).
    """

    model_config = ConfigDict(frozen=True)

    status: SuggestionStatus | None = Field(default=None, description="Only rows in this status.")
    include_obsolete: bool = Field(
        default=False,
        description=(
            "Include OBSOLETE rows when no explicit status filter is set. They are hidden "
            "by default because a refresh can retire many rows at once; picking the "
            "obsolete status explicitly always shows them."
        ),
    )
    op: str | None = Field(default=None, description="Only rows of this op.")
    property_name: str | None = Field(default=None, description="Only rows for this property (or '_title').")
    match: Literal["exact", "mismatch", "new"] | None = Field(
        default=None,
        description=(
            "Only rows with this match verdict: 'exact' (proposed equals current), "
            "'mismatch' (differs), 'new' (no current value to compare). None = no filter. "
            "A three-way literal rather than a bool because 'new' is a distinct, "
            "operator-meaningful state, not the absence of a filter."
        ),
    )
    text: str | None = Field(default=None, description="Case-insensitive LIKE over title + sharedId.")
    accepted_only: bool = Field(
        default=False,
        description=(
            "Restrict to rows the apply path would push: ACCEPTED or EDITED. Used by "
            "'Accept selected' so the button can reuse the current filter."
        ),
    )
    shared_ids: list[str] | None = Field(
        default=None,
        description=(
            "Restrict to these sharedIds (every language of them). An explicit "
            "empty list matches NOTHING — 'nothing selected' is distinct from 'no constraint'."
        ),
    )
    entity_languages: list[tuple[str, str | None]] | None = Field(
        default=None,
        description=(
            "Restrict to these (sharedId, language) PAIRS — the table's checkbox "
            "selection, whose rows are per language. A pair with None language "
            "means the language-independent group. An explicit empty list matches "
            "NOTHING, as with shared_ids."
        ),
    )
    languages: list[str] | None = Field(
        default=None,
        description=(
            "Restrict to rows written in these languages (the table's Language filter). "
            "The language-independent rows (language IS NULL) are never matched by it — "
            "they are their own group. An explicit empty list matches NOTHING."
        ),
    )
    suggestion_ids: list[int] | None = Field(
        default=None,
        description=(
            "Restrict to these suggestion row ids — the SUBOPERATION selection, which is "
            "how the review table's checked parts reach the store. An explicit empty list "
            "matches NOTHING (nothing checked is distinct from no constraint)."
        ),
    )
    limit: int = Field(default=25, ge=1, description="Page size (ignored by aggregate/projection queries).")
    offset: int = Field(default=0, ge=0, description="Page offset (ignored by aggregate/projection queries).")


class PredictionAttempt(BaseModel):
    """One prediction pass: its budget, spend, progress, and outcome.

    The UI polls this row while a pass runs (``entities_done`` vs
    ``entities_total``) and reads it afterwards for the spent-vs-budget badge —
    including the ``ABORTED`` case, where the spend is exactly why the pass
    stopped.
    """

    model_config = ConfigDict(frozen=True)

    run_id: str = Field(description="The run the pass belongs to.")
    attempt: int = Field(description="The attempt number (1-based, per run).")
    status: PredictionAttemptStatus = Field(default=PredictionAttemptStatus.RUNNING, description="Pass lifecycle state.")
    budget: LlmBudget = Field(description="The approved ceiling this pass ran under.")
    spent: LlmUsage = Field(default_factory=LlmUsage, description="Real consumption recorded by the ledger.")
    entities_total: int = Field(default=0, description="How many entities the pass intends to cover.")
    entities_done: int = Field(default=0, description="How many entities have been processed so far.")
    entity_scope: list[str] | None = Field(
        default=None,
        description=(
            "The sharedIds a subset pass was restricted to (None = the script's own "
            "discovery, i.e. the whole instance it selects)."
        ),
    )
    error: str | None = Field(default=None, description="Why the pass aborted/failed, when it did.")
    created_at: datetime | None = Field(default=None, description="When the pass started.")
    finished_at: datetime | None = Field(default=None, description="When the pass reached a terminal state.")


def entity_language_key(shared_id: str, language: str | None) -> str:
    """One review-table row's identity: the entity plus its language (pure).

    The table's rows are per (entity, language), so the checkbox, the Quasar
    ``row_key`` and the store's per-group projection all need the same flat key.
    The language-independent group keys as an empty language, which keeps it a
    distinct row rather than colliding with any language.
    """
    return f"{shared_id}|{language or ''}"


__all__ = [
    "CREATE_OPS",
    "EntityReviewRow",
    "MERGE_OPS",
    "OperationReviewRow",
    "PROPERTY_OPS",
    "PredictionAttempt",
    "PredictionAttemptStatus",
    "Suggestion",
    "SuggestionFilter",
    "SuggestionOp",
    "SuggestionStatus",
    "TITLE_PROPERTY",
    "UPDATE_OPS",
    "entity_language_key",
]
