"""Turn dry-run records into reviewable suggestions, and score them (pure).

Two responsibilities, both pure and both unit-tested in isolation:

- :func:`records_to_suggestions` — the conversion from the dry-run recorder's
  per-op dicts into :class:`Suggestion` rows. This is where the op vocabulary
  is normalized into a table the operator reviews: an ``update``/``create``
  record explodes into one row per metadata key (plus a ``_title`` row when the
  record carries a title), a whole-entity op yields exactly one row.
- :func:`compare_values` / :func:`score_accuracy` — accuracy against Uwazi's
  current values, so the operator can see WHERE the script's predictions fail
  before applying anything.

The conversion lives here rather than in the use case so it is testable from
literal record dicts with no ports, no I/O, and no running instance.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from uwazi_admin_agent.domain.metadata_diff import format_metadata_value
from uwazi_admin_agent.domain.suggestion import (
    CREATE_OPS,
    PROPERTY_OPS,
    TITLE_PROPERTY,
    UPDATE_OPS,
    Suggestion,
    SuggestionOp,
    SuggestionStatus,
)

# Whole-entity ops (no per-property breakdown): one suggestion per record.
_WHOLE_ENTITY_OPS: frozenset[str] = frozenset(
    {"delete", "publish", "unpublish", "set_publish_status", "create_relationships"}
)


@dataclass(frozen=True)
class MergeOperation:
    """One scripted merge: a target write plus the sources deleted by that write.

    ``target_op`` is the base record op the target was written with (``update``
    or ``create``) — it decides which of the two merge ops the review row gets.
    """

    target_shared_id: str
    target_op: str
    source_shared_ids: tuple[str, ...]


def _record_shared_id(record: dict[str, Any], index: int) -> str:
    """The sharedId a record addresses, resolving a create's placeholder (pure).

    A create record has no real sharedId before the write, so the dry-run
    recorder stamps its return placeholder (``dry-created-N``); records from
    older scripts without that field fall back to the record's position, which
    is the same placeholder the recorder minted.
    """
    shared_id = str(record.get("shared_id") or "")
    if not shared_id and record.get("op") == "create":
        return f"dry-created-{index}"
    return shared_id


def merge_operations(records: list[dict[str, Any]]) -> dict[str, MergeOperation]:
    """The merge actions a script's records describe, keyed by target sharedId (pure).

    A merge script writes the target (``update_entities``/``create_entities``),
    moves the sources' files to it (``move_files``), then deletes the sources.
    The file move is the composition's explicit grouping signal: its
    ``to_shared_id`` names the target and its ``from_shared_ids`` the sources.
    Only a target that ALSO has an update/create record is a merge — a bare
    move+delete stays an ordinary delete plan, because no target is written.

    Several moves/targets in one script (a multi-group merge) yield one entry
    per target, preserving the script's move order.
    """
    normalized: list[tuple[dict[str, Any], str]] = [
        (record, _record_shared_id(record, index)) for index, record in enumerate(records)
    ]
    sources_by_target: dict[str, list[str]] = {}
    order: list[str] = []
    for record, _shared_id in normalized:
        if record.get("op") != "move_files":
            continue
        target = str(record.get("to_shared_id") or "")
        raw_sources = record.get("from_shared_ids")
        if not target or not isinstance(raw_sources, (list, tuple)):
            continue
        if target not in sources_by_target:
            sources_by_target[target] = []
            order.append(target)
        for source in raw_sources:
            source_id = str(source)
            if source_id and source_id != target and source_id not in sources_by_target[target]:
                sources_by_target[target].append(source_id)

    target_ops: dict[str, str] = {}
    for record, shared_id in normalized:
        op = str(record.get("op") or "")
        if op not in ("update", "create") or shared_id not in sources_by_target:
            continue
        # First target record wins; a normal merge has exactly one.
        target_ops.setdefault(shared_id, op)

    return {
        target: MergeOperation(
            target_shared_id=target,
            target_op=target_ops[target],
            source_shared_ids=tuple(sources_by_target[target]),
        )
        for target in order
        if target in target_ops and sources_by_target[target]
    }


def _as_op(raw: Any) -> SuggestionOp | None:
    """Coerce a record's ``op`` label into a known :data:`SuggestionOp` (None = unknown).

    ``move_files`` and ``delete_file`` records are deliberately NOT suggestions:
    they are file-collection operations the apply path does not push as entity
    property values (the dry-run report counts them for the step's implication
    preview instead).
    """
    value = str(raw or "")
    if value in (
        "update",
        "create",
        "delete",
        "publish",
        "unpublish",
        "set_publish_status",
        "create_relationships",
        "update_and_delete",
        "create_and_delete",
    ):
        return value  # type: ignore[return-value] — the literal set above matches SuggestionOp exactly
    return None


def row_language(value_language: str | None, record_language: Any) -> str | None:
    """The language a review row is filed under (pure).

    A value carrying its own ``{"value", "language"}`` entries decides by itself
    — that is how a translation task splits into one row per language — and it
    wins whenever it says anything.

    Otherwise the row falls back to the ``language`` the SCRIPT passed to
    ``update_entities(..., language="es")``. That argument is the only carrier of
    the intent when the value is a bare scalar, which is exactly what the script
    contract mandates for ``text``/``markdown`` ("a string", system prompt §
    METADATA WRITE SHAPE) and what the mapper then wraps as a single
    ``[{"value": ...}]`` entry. Dropping it filed "update the Document Ref in
    Spanish" under the language-agnostic "—" group, where it neither reads as the
    Spanish row it is nor can be checked apart from its siblings.
    """
    if value_language:
        return value_language
    if isinstance(record_language, str) and record_language:
        return record_language
    return None


def records_to_suggestions(
    run_id: str,
    attempt: int,
    records: list[dict[str, Any]],
    entity_titles: dict[str, str] | None = None,
    now: datetime | None = None,
) -> list[Suggestion]:
    """Convert dry-run records into suggestion rows (pure).

    Per-op expansion:

    - ``update`` / ``update_and_delete``: one row per metadata key, SPLIT per
      language for a per-language property (a value list carrying several
      ``{"value", "language"}`` entries becomes one row per language, each
      holding just its own entry), plus one ``_title`` row when the record
      carries a non-None ``title``. Records with no metadata and no title
      produce a single row with ``property_name=None`` so the operator still
      sees the no-op touch.
    - ``create`` / ``create_and_delete``: the same expansion, with the
      recorder's placeholder ``dry-created-N`` sharedId (creates have no real
      sharedId until applied).
    - ``delete`` / ``publish`` / ``unpublish`` / ``set_publish_status`` /
      ``create_relationships``: one row each, ``property_name=None``.

    A scripted merge (a target update/create plus a file move plus the source
    deletes) is collapsed HERE: the target's rows carry the merge op
    (``update_and_delete``/``create_and_delete``) and the moved sources, and the
    source delete records are dropped. That is what makes a merge ONE table row
    — one target, however many sources it absorbs — instead of a target row
    followed by one delete row per source. See :func:`merge_operations`.

    The split is what makes a translation task reviewable: the operator sees one
    row per language, checks the one they want written, and Run pushes only that
    language's values (the apply path merges the entity's other languages back
    in, since Uwazi replaces a property's whole value list on write). A
    language-independent value — a select, a multiselect, a link, a date, a
    title — stays ONE row with ``language=None``.

    ``entity_titles`` maps sharedId → title for display (the prediction use case
    resolves it from the entities it already read). Order follows the records so
    the review table's unsorted view mirrors the script's execution order. Rows
    carry the record's ``template_name`` when the script recorded one (the
    review table's Template column and the Run executor's write payloads).

    A property row's language comes from the VALUE when the value carries one,
    and otherwise from the record's ``language`` — the argument the script passed
    to the write helper (see :func:`row_language`). Without that fallback a
    script that writes a per-language property as the contract's bare scalar
    (``text``/``markdown`` → ``"a string"``) and passes ``language="es"`` landed
    in the language-agnostic "—" group, so the Spanish row of a translation task
    was indistinguishable from a language-independent change.
    """
    titles = entity_titles or {}
    created = now or datetime.now(timezone.utc)
    suggestions: list[Suggestion] = []
    merges = merge_operations(records)
    merge_sources = {source for merge in merges.values() for source in merge.source_shared_ids}

    for index, record in enumerate(records):
        op = _as_op(record.get("op"))
        if op is None:
            continue
        shared_id = _record_shared_id(record, index)
        if op == "delete" and shared_id in merge_sources:
            # The source is absorbed by its target's merge row: a second delete
            # row would offer the operator a partial action that cannot run on
            # its own (the target write is what makes the merge meaningful).
            continue
        merge = merges.get(shared_id)
        if merge is not None and op in UPDATE_OPS | CREATE_OPS:
            op = "update_and_delete" if merge.target_op == "update" else "create_and_delete"
        title = titles.get(shared_id, "") or str(record.get("title") or "")
        base: dict[str, Any] = {
            "run_id": run_id,
            "attempt": attempt,
            "shared_id": shared_id,
            "entity_title": title,
            "template_name": str(record.get("template_name") or "") or None,
            "op": op,
            "status": SuggestionStatus.PENDING,
            "created_at": created,
            "merge_source_ids": list(merge.source_shared_ids) if merge is not None else [],
        }

        if op in PROPERTY_OPS:
            record_metadata = record.get("metadata")
            metadata: dict[str, Any] = record_metadata if isinstance(record_metadata, dict) else {}
            record_language = record.get("language")
            emitted = False
            if record.get("title") is not None:
                suggestions.append(Suggestion(**base, property_name=TITLE_PROPERTY, proposed_value=record.get("title")))
                emitted = True
            for key, value in metadata.items():
                for language, split_value in per_language_split(value):
                    suggestions.append(
                        Suggestion(
                            **base,
                            language=row_language(language, record_language),
                            property_name=str(key),
                            proposed_value=split_value,
                        )
                    )
                emitted = True
            if not emitted:
                suggestions.append(Suggestion(**base, property_name=None, proposed_value=None))
            continue

        proposed: Any = None
        if op == "set_publish_status":
            proposed = bool(record.get("published"))
        suggestions.append(Suggestion(**base, property_name=None, proposed_value=proposed))

    return suggestions


def value_in_language(value: Any, language: str | None) -> Any:
    """The part of ``value`` that belongs to one language (pure).

    A per-language row compares against the CURRENT value of ITS language only,
    never the whole list — otherwise every translated row would look like a
    mismatch against its siblings. A language the entity does not carry yet
    yields None, which the match computation reads as "new" (nothing to
    compare) rather than a difference. ``language=None`` (a language-independent
    row) gets the value untouched.

    Uwazi stores a translated property as ONE entity-language ROW whose value
    entry carries no ``language`` key (the ``locale`` cookie already picked the
    row), so a value with no tagged entry is the row's own value and is returned
    whole. Narrowing it against a ``language`` no entry carries would report
    every real text row as "new" — i.e. as an addition rather than the
    comparison it is. A value that DOES tag its entries (a recorded multi-language
    proposal, a legacy store) keeps the per-language narrowing.
    """
    if language is None or not isinstance(value, (list, tuple)):
        return value
    tagged = [entry for entry in value if isinstance(entry, dict) and entry.get("language")]
    if not tagged:
        return value
    entries = [entry for entry in value if isinstance(entry, dict) and entry.get("language") == language]
    return entries or None


def per_language_split(value: Any) -> list[tuple[str | None, Any]]:
    """One ``(language, value)`` pair per language a value carries (pure).

    A per-language property (text, richtext) is stored as a list of
    ``{"value", "language"}`` entries, so a translation task records ONE property
    row whose value carries several languages. The review table has to show one
    row per language, which is what this returns — each pair holding ONLY that
    language's entry. A language-independent value (a select, a multiselect, a
    link, a date) comes back as a single ``(None, value)`` pair.
    """
    languages = value_languages(value)
    if not languages or not isinstance(value, (list, tuple)):
        return [(None, value)]
    entries = [entry for entry in value if isinstance(entry, dict)]
    return [(language, [entry for entry in entries if entry.get("language") == language]) for language in languages]


def value_languages(value: Any) -> list[str]:
    """The languages a Uwazi metadata value carries, in first-appearance order.

    Uwazi stores a per-language property (text, richtext) as a list of
    ``{"value": ..., "language": ...}`` entries, while a language-INDEPENDENT
    one (select, multiselect, link, date, ...) is a bare scalar/list — so an
    empty result means "this value has no language", not "unknown". Entries
    without a ``language`` key never contribute one. Pure.
    """
    languages: list[str] = []
    if not isinstance(value, (list, tuple)):
        return languages
    for entry in value:
        if not isinstance(entry, dict):
            continue
        language = entry.get("language")
        if isinstance(language, str) and language and language not in languages:
            languages.append(language)
    return languages


def value_language(value: Any) -> str | None:
    """The ONE language a value is written in, or None when it is agnostic.

    A value that carries several languages (a translation task's per-language
    text) is ambiguous by construction and reports None, which is what keeps
    such a row in the language-agnostic "—" group instead of claiming the
    first language it happens to list.
    """
    languages = value_languages(value)
    return languages[0] if len(languages) == 1 else None


def compare_values(proposed: Any, current: Any) -> bool | None:
    """Whether ``proposed`` matches ``current``, or None when there is nothing to compare.

    ``None`` current (missing key / absent entity value) means the suggestion
    ADDS a value — that is not a mismatch, so it scores as ``None`` ("new").
    Both sides are rendered through
    :func:`~uwazi_admin_agent.domain.metadata_diff.format_metadata_value` before
    comparison, so a list-vs-joined-text difference is never reported as a
    change (the same display-equality rule the results window uses). Pure.
    """
    if current is None:
        return None
    return format_metadata_value(proposed) == format_metadata_value(current)


class PropertyAccuracy(BaseModel):
    """Per-property accuracy counters (matched / mismatched / new)."""

    model_config = ConfigDict(frozen=True)

    property_name: str
    matched: int = 0
    mismatched: int = 0
    new: int = 0


class AccuracySummary(BaseModel):
    """Aggregate accuracy of a prediction set, overall plus per property."""

    model_config = ConfigDict(frozen=True)

    matched: int = 0
    mismatched: int = 0
    new: int = 0
    by_property: dict[str, PropertyAccuracy] = Field(default_factory=dict)

    @property
    def accuracy(self) -> float | None:
        """matched / (matched + mismatched), or None when nothing is comparable."""
        denominator = self.matched + self.mismatched
        if denominator <= 0:
            return None
        return self.matched / denominator


def score_accuracy(suggestions: list[Suggestion]) -> AccuracySummary:
    """Score a suggestion set against Uwazi's current values (pure).

    Only rows that carry a match verdict (property updates with a current value)
    move the counters; whole-entity ops and new values are counted separately.
    Property names are reported as-is (``_title`` for title changes), which is
    exactly the key the review UI's property filter uses.
    """
    by_property: dict[str, PropertyAccuracy] = {}
    matched = mismatched = new = 0

    for suggestion in suggestions:
        key = suggestion.property_name
        if key is None:
            continue
        stats = by_property.get(key) or PropertyAccuracy(property_name=key)
        if suggestion.match is True:
            matched += 1
            stats = stats.model_copy(update={"matched": stats.matched + 1})
        elif suggestion.match is False:
            mismatched += 1
            stats = stats.model_copy(update={"mismatched": stats.mismatched + 1})
        else:
            new += 1
            stats = stats.model_copy(update={"new": stats.new + 1})
        by_property[key] = stats

    return AccuracySummary(matched=matched, mismatched=mismatched, new=new, by_property=by_property)


__all__ = [
    "AccuracySummary",
    "MergeOperation",
    "PropertyAccuracy",
    "compare_values",
    "merge_operations",
    "per_language_split",
    "records_to_suggestions",
    "row_language",
    "score_accuracy",
    "value_in_language",
    "value_language",
    "value_languages",
]
