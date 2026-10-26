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
from uwazi_admin_agent.domain.property_language_scope import is_per_language_property_type
from uwazi_admin_agent.domain.suggestion import (
    CREATE_OPS,
    INFO_OP,
    MERGE_OPS,
    MOVE_OP,
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
    """One scripted merge: a target absorbing the sources deleted by the move.

    ``target_op`` is the base record op the target was written with (``update``
    or ``create``) — it decides which of the two merge ops the review row gets —
    and is ``None`` when the script wrote no target metadata (a "just append the
    files and remove the sources" plan). That case is still ONE atomic action:
    the file copy and the source deletes belong together, so the review row is a
    propertyless anchor (see :func:`records_to_suggestions`).
    ``files`` is the dry run's best-effort snapshot of the uploaded files the
    move would copy (``from_shared_id`` + the file-ref fields); it is display
    data only, because the apply path re-reads the live files.
    """

    target_shared_id: str
    target_op: str | None
    source_shared_ids: tuple[str, ...]
    files: tuple[dict[str, Any], ...] = ()


def _merge_files(record: dict[str, Any], sources: list[str]) -> list[dict[str, Any]]:
    """The file-ref dicts one ``move_files`` record carries for its sources (pure).

    The dry-run mover records what it READ from each source; older scripts (and
    a pass with no entity repository) record no ``files`` list, which stays an
    empty list — the review then falls back to a per-source "uploaded files"
    row instead of claiming file names it never saw.
    """
    raw_files = record.get("files")
    if not isinstance(raw_files, (list, tuple)):
        return []
    known_sources = set(sources)
    files: list[dict[str, Any]] = []
    for entry in raw_files:
        if not isinstance(entry, dict):
            continue
        source = str(entry.get("from_shared_id") or "")
        if source and source not in known_sources:
            continue
        normalized = {str(key): value for key, value in entry.items()}
        if source:
            normalized["from_shared_id"] = source
        files.append(normalized)
    return files


def _move_sources(record: dict[str, Any], target: str) -> list[str]:
    """The distinct, non-target source ids one ``move_files`` record names (pure)."""
    raw_sources = record.get("from_shared_ids")
    if not isinstance(raw_sources, (list, tuple)):
        return []
    sources: list[str] = []
    for source in raw_sources:
        source_id = str(source)
        if source_id and source_id != target and source_id not in sources:
            sources.append(source_id)
    return sources


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
    if not shared_id and record.get("op") == MOVE_OP:
        # A move addresses its TARGET (the entity that gains the files). The
        # sources travel in the row's merge_source_ids.
        return str(record.get("to_shared_id") or "")
    if not shared_id and record.get("op") == INFO_OP:
        return f"info-{index}"
    return shared_id


def merge_operations(records: list[dict[str, Any]]) -> dict[str, MergeOperation]:
    """The merge actions a script's records describe, keyed by target sharedId (pure).

    A merge script writes the target (``update_entities``/``create_entities``)
    **or not**, moves the sources' files to it (``move_files``), then deletes the
    sources. The file move is the composition's explicit grouping signal: its
    ``to_shared_id`` names the target and its ``from_shared_ids`` the sources.

    What makes a merge is the file move PLUS a delete for EVERY moved source:
    the copy and the deletes are one atomic action, and running the deletes
    without the copy would throw the sources' files away. A target write is
    optional — "append these files and remove the sources" with no metadata
    change is still one operation (a propertyless anchor), not a copy row plus
    separate delete rows the operator could run in the wrong order.

    A move without a source delete is a plain file copy (the operator asked to
    keep the source) — never a merge. Treating that as a merge would delete a
    source the script never asked to delete. A move that deletes only SOME
    sources is also not a merge: the kept sources still need their files copied,
    so the plan stays an explicit copy row plus the requested delete rows.

    Several moves/targets in one script (a multi-group merge) yield one entry
    per target, preserving the script's move order.
    """
    normalized: list[tuple[dict[str, Any], str]] = [
        (record, _record_shared_id(record, index)) for index, record in enumerate(records)
    ]
    deleted_ids = {shared_id for record, shared_id in normalized if str(record.get("op") or "") == "delete" and shared_id}
    sources_by_target: dict[str, list[str]] = {}
    move_records_by_target: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for record, _shared_id in normalized:
        if record.get("op") != MOVE_OP:
            continue
        target = str(record.get("to_shared_id") or "")
        raw_sources = record.get("from_shared_ids")
        if not target or not isinstance(raw_sources, (list, tuple)):
            continue
        if target not in sources_by_target:
            sources_by_target[target] = []
            move_records_by_target[target] = []
            order.append(target)
        move_records_by_target[target].append(record)
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

    merges: dict[str, MergeOperation] = {}
    for target in order:
        sources = sources_by_target[target]
        if not sources or target in deleted_ids:
            # Nothing moved, or the "target" is itself being deleted: no merge.
            continue
        if any(source not in deleted_ids for source in sources):
            # The script moved files from a source but did not delete it: the
            # operator wants the source kept, so this is a file copy, not a
            # merge. The target update (if any) stays a normal update row and
            # the move becomes its own row (see records_to_suggestions).
            continue
        files: list[dict[str, Any]] = []
        seen_files: set[tuple[str, str, str]] = set()
        for record in move_records_by_target[target]:
            for file in _merge_files(record, sources):
                identity = (
                    str(file.get("from_shared_id") or ""),
                    str(file.get("kind") or ""),
                    str(file.get("file_id") or file.get("filename") or file.get("originalname") or ""),
                )
                if identity in seen_files:
                    continue
                seen_files.add(identity)
                files.append(file)
        merges[target] = MergeOperation(
            target_shared_id=target,
            target_op=target_ops.get(target),
            source_shared_ids=tuple(sources),
            files=tuple(files),
        )
    return merges


def _as_op(raw: Any) -> SuggestionOp | None:
    """Coerce a record's ``op`` label into a known :data:`SuggestionOp` (None = unknown).

    ``move_files`` and ``delete_file`` records: ``move_files`` becomes its OWN
    suggestion when it is not part of a merge (a file copy the operator asked to
    keep the source for) — otherwise it is collapsed into the merge row.
    ``delete_file`` records are not suggestions: they are file-collection
    operations whose undo is a re-upload, not an un-record. ``info`` IS
    accepted: it is recorded as a suggestion so the review table can display it,
    and ``op == INFO_OP`` is what tells every write path to ignore it.
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
        MOVE_OP,
        "update_and_delete",
        "create_and_delete",
        INFO_OP,
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


def _is_per_language_property(
    property_name: str,
    value: Any,
    property_types: dict[str, str] | None,
) -> bool:
    """Whether a property's value is stored PER LANGUAGE ROW (pure).

    The instance's template definitions answer it: ``property_types`` maps a
    property NAME to its Uwazi type, and only the free-text types are stored
    per language row (see
    :mod:`uwazi_admin_agent.domain.property_language_scope`).

    Without a declared type the value's own shape is the next best answer: a
    value carrying ``{"value", "language"}`` entries is a per-language value by
    construction.

    With NO types at all (the lookup unwired, the instance unreachable) the
    value's shape is not enough — the contract writes a text value as a bare
    scalar too, so a bare value is ambiguous — and the row keeps the script's
    ``language`` argument, which is what the recording path has always done.
    """
    declared = (property_types or {}).get(property_name)
    if declared:
        return is_per_language_property_type(declared)
    if property_types:
        # The instance's types were read and this property is not among them:
        # a bare value is then a shared one (a select label, a list of labels,
        # a date), and only a value naming its own languages is per-language.
        return bool(value_languages(value))
    # No types at all: keep the existing behaviour — the script's language
    # argument carries the intent.
    return True


def _record_shared_change(
    suggestions: list[Suggestion],
    base: dict[str, Any],
    *,
    property_name: str,
    proposed_value: Any,
) -> None:
    """File ONE row for a language-independent change, replacing any earlier one.

    A shared value has no language, so its row's identity is the entity + the
    property — and a script that wrote the same shared property once per
    language produces the SAME change several times. Filing each of them would
    offer the operator two (or more) rows for one change: they would be
    checked separately, run separately, and the second write would replace the
    first with the same value. So the LAST write wins — it is the one the
    script settled on — and it replaces the earlier row in place.
    """
    for index, existing in enumerate(suggestions):
        if (
            existing.shared_id == base["shared_id"]
            and existing.op == base["op"]
            and existing.property_name == property_name
            and existing.language is None
        ):
            suggestions[index] = Suggestion(
                **base,
                language=None,
                property_name=property_name,
                proposed_value=proposed_value,
            )
            return
    suggestions.append(
        Suggestion(
            **base,
            language=None,
            property_name=property_name,
            proposed_value=proposed_value,
        )
    )


def records_to_suggestions(
    run_id: str,
    records: list[dict[str, Any]],
    entity_titles: dict[str, str] | None = None,
    now: datetime | None = None,
    property_types: dict[str, str] | None = None,
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
    - ``move_files`` that is NOT part of a merge: one row on the TARGET entity,
      carrying the sources in ``merge_source_ids`` and the files it would copy
      in ``merge_files``. This is a file copy (the sources keep their files),
      so it is a first-class runnable operation — a plan whose only work is
      "append these files to that entity" must not be unreviewable.
    - ``info``: ONE parent row carrying the title, plus one child row per
      entry in ``children``. They are display-only: the review table shows the
      title as the operation and the children as its suboperations, with no
      action buttons. The parent and children share a synthetic ``info-N``
      sharedId (so they group into one operation) and the children carry an
      ``info-child-N`` detail key (so the store's row identity stays unique).
      When the SAME script records change operations, info rows are DROPPED
      unless the record is marked ``keep=True``: a change plan must explain
      itself through its operations, and an info row restating them is noise.
      ``keep=True`` is reserved for information no change row can show (failed
      or skipped entities, or an explicitly requested answer).

    A scripted merge — a file move plus a delete for EVERY moved source, with or
    without a target write — is collapsed HERE: the target's rows carry the
    merge op (``update_and_delete``/``create_and_delete``) and the moved
    sources, and the source delete records are dropped. When the script wrote no
    target metadata, one propertyless anchor row is emitted at the move's
    position so the copy and the deletes still run as one operation. That is
    what makes a merge ONE table row — one target, however many sources it
    absorbs — instead of a target row followed by one delete row per source.
    See :func:`merge_operations`.

    The split is what makes a translation task reviewable: the operator sees one
    row per language, checks the one they want written, and Run pushes only that
    language's values (the apply path merges the entity's other languages back
    in, since Uwazi replaces a property's whole value list on write). A
    language-independent value — a select, a multiselect, a link, a date, a
    title — stays ONE row with ``language=None``.

    ``property_types`` maps a property NAME to its Uwazi type ("text",
    "multiselect", ...), read from the instance's template definitions. It is
    what makes a language-independent change ONE suboperation even when the
    script wrote it several times: a multiselect change recorded under
    ``language="en"`` and again under ``language="es"`` is ONE change to ONE
    shared value, so it must not become two rows the operator can run
    separately (the second would overwrite the first with the same value). Only
    the free-text types are stored per language row, so only those are split
    (see :mod:`uwazi_admin_agent.domain.property_language_scope`). Without the
    types the recording falls back to the VALUE's shape: a value carrying
    ``{"value", "language"}`` entries is split, and a bare value is not.

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
    anchored_targets: set[str] = set()
    # A plan that records ANY change explains itself through its operations.
    # Info rows in such a plan are dropped unless explicitly marked ``keep``, so
    # a stray "moved 2 files" summary cannot bury the actual change rows.
    has_changes = any(_as_op(record.get("op")) not in (None, INFO_OP) for record in records)

    for index, record in enumerate(records):
        op = _as_op(record.get("op"))
        if op is None:
            continue
        shared_id = _record_shared_id(record, index)
        if op == "delete" and shared_id in merge_sources:
            # The source is absorbed by its target's merge row: the copy and the
            # delete are one atomic action, so a separate delete row would offer
            # a partial action that can run in the wrong order (or alone).
            continue
        merge = merges.get(shared_id)
        if merge is not None and op in UPDATE_OPS | CREATE_OPS:
            op = "update_and_delete" if merge.target_op == "update" else "create_and_delete"
        title = titles.get(shared_id, "") or str(record.get("title") or "")
        base: dict[str, Any] = {
            "run_id": run_id,
            "shared_id": shared_id,
            "entity_title": title,
            "template_name": str(record.get("template_name") or "") or None,
            "op": op,
            "status": SuggestionStatus.PENDING,
            "created_at": created,
            "merge_source_ids": list(merge.source_shared_ids) if merge is not None else [],
            "merge_files": [dict(file) for file in merge.files] if merge is not None else [],
        }

        if op == MOVE_OP:
            sources = _move_sources(record, shared_id)
            if merge is not None:
                # The move belongs to a merge: its copy is part of the merge row
                # (the target's property rows when it is written). A merge with
                # NO target write still needs one propertyless anchor to carry
                # the copy and the source deletes together — without it the plan
                # would degrade to a copy row plus independently runnable delete
                # rows, and running the deletes alone would lose the files.
                if merge.target_op is None and shared_id not in anchored_targets:
                    anchored_targets.add(shared_id)
                    suggestions.append(
                        Suggestion(
                            **{**base, "op": "update_and_delete"},
                            property_name=None,
                            proposed_value=None,
                        )
                    )
                continue
            if not sources:
                # Nothing to copy: no standalone operation to review.
                continue
            suggestions.append(
                Suggestion(
                    **{
                        **base,
                        "merge_source_ids": sources,
                        "merge_files": _merge_files(record, sources),
                    },
                    property_name=None,
                    proposed_value=None,
                    detail_key=",".join(sources),
                )
            )
            continue

        if op == INFO_OP:
            # DISPLAY-ONLY. ``add_info(title, children)`` records one parent
            # carrying the headline and one child row per detail line. They
            # share the synthetic ``shared_id`` so the store groups them into
            # one operation; the children's ``detail_key`` keeps their DB
            # identity distinct from the parent (which has None). Neither row
            # writes anything: the review table renders them without actions.
            #
            # In a plan that also records changes, a summary row is noise: the
            # operations ARE the summary. Only an explicitly kept row (a
            # failure/skip list or an asked-for answer no change row shows)
            # survives; this is what stops a script from burying the changes
            # under an "info" paragraph that the operator must read.
            if has_changes and not record.get("keep"):
                continue
            suggestions.append(Suggestion(**base, property_name=None, proposed_value=title))
            raw_children = record.get("children")
            children = raw_children if isinstance(raw_children, (list, tuple)) else []
            for child_index, child in enumerate(children):
                suggestions.append(
                    Suggestion(
                        **base,
                        property_name=None,
                        proposed_value="" if child is None else str(child),
                        detail_key=f"info-child-{child_index}",
                    )
                )
            continue

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
                    # A property whose type is LANGUAGE-INDEPENDENT holds ONE
                    # value shared by every language row, so it is ONE change:
                    # it is never filed under a language, and it is never
                    # split. The script may well have written it once per
                    # language (the helper's ``language`` argument selects the
                    # ROW, not the value) — that is still one change to one
                    # shared value, and filing it twice would offer the
                    # operator two rows that overwrite each other.
                    if not _is_per_language_property(key, value, property_types):
                        _record_shared_change(
                            suggestions,
                            base,
                            property_name=str(key),
                            proposed_value=split_value,
                        )
                        continue
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
        if op == "create_relationships":
            # ONE SUBOPERATION PER LINK. The dry-run recorder writes one record
            # per relationship (``{"op": "create_relationships", **rel}``), and
            # the apply path expects that very dict back in ``proposed_value``
            # (``_write`` builds its ``create_relationships`` payload from it) —
            # dropping it made every relationship row a no-op write. The link's
            # target is the row's ``detail_key``: the batch's rows all share the
            # entity, op, language and (null) property, so without it the store's
            # identity would collapse N links into one.
            rel = {key: value for key, value in record.items() if key != "op"}
            origin = str(rel.get("from_entity_shared_id") or rel.get("shared_id") or shared_id)
            target = str(rel.get("to_entity_shared_id") or rel.get("to_shared_id") or "")
            suggestions.append(
                Suggestion(
                    **{**base, "shared_id": origin},
                    property_name=None,
                    proposed_value=rel,
                    detail_key=target or None,
                )
            )
            continue
        suggestions.append(Suggestion(**base, property_name=None, proposed_value=proposed))

    return suggestions


def drop_unchanged_property_rows(suggestions: list[Suggestion]) -> list[Suggestion]:
    """Remove property rows whose proposed value EQUALS the current value (pure).

    Hydration fills ``match`` after the records are converted, so this runs on
    the hydrated rows. A no-op property row is not a change: showing it offers
    the operator a "value → same value" suboperation that says nothing, and
    pushing it would send a pointless write. Such rows are dropped here at the
    source instead of being hidden in one of the two review surfaces.

    A merge whose target writes are ALL no-ops keeps ONE propertyless anchor
    row. The merge still has two real actions — copying the sources' files and
    deleting the sources — and :func:`merge_deletions` needs a row to carry
    ``merge_source_ids``. The anchor renders as the merge operation itself; it
    writes no property value.
    """
    groups_needing_anchor: set[tuple[str, str]] = set()
    kept_groups: set[tuple[str, str]] = set()
    for suggestion in suggestions:
        if suggestion.op not in MERGE_OPS:
            continue
        key = (suggestion.shared_id, suggestion.op)
        unchanged = suggestion.property_name is not None and suggestion.match is True
        (kept_groups if not unchanged else groups_needing_anchor).add(key)
    anchors = groups_needing_anchor - kept_groups
    anchored: set[tuple[str, str]] = set()

    kept: list[Suggestion] = []
    for suggestion in suggestions:
        unchanged = suggestion.op in PROPERTY_OPS and suggestion.property_name is not None and suggestion.match is True
        if not unchanged:
            kept.append(suggestion)
            continue
        if suggestion.op not in MERGE_OPS:
            continue
        key = (suggestion.shared_id, suggestion.op)
        if key not in anchors or key in anchored:
            continue
        anchored.add(key)
        kept.append(
            suggestion.model_copy(
                update={
                    "property_name": None,
                    "language": None,
                    "detail_key": None,
                    "proposed_value": None,
                    "current_value": None,
                    "match": None,
                }
            )
        )
    return kept


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
    change (the same display-equality rule the review table's match uses). Pure.
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
    "drop_unchanged_property_rows",
    "merge_operations",
    "per_language_split",
    "records_to_suggestions",
    "row_language",
    "score_accuracy",
    "value_in_language",
    "value_language",
    "value_languages",
]
