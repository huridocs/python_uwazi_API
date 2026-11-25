"""Pure apply-payload builders (moved verbatim from apply_suggestions_use_case)."""

from __future__ import annotations

import json
from typing import Any

from uwazi_admin_agent.domain.date_values import coerce_date_property_value, is_date_property_type
from uwazi_admin_agent.domain.relationship_values import (
    RELATIONSHIP_TYPE,
    relationship_selection,
    single_value,
)
from uwazi_admin_agent.domain.select_values import (
    SELECT_TYPES,
    UnknownThesaurusValue,
    labels_of,
    select_value_envelope,
)
from uwazi_admin_agent.domain.suggestion import (
    CREATE_OPS,
    DISPLAY_ONLY_OPS,
    FILE_OPS,
    MERGE_OPS,
    MOVE_OP,
    TITLE_PROPERTY,
    UPDATE_OPS,
    Suggestion,
    SuggestionStatus,
)
from uwazi_admin_agent.domain.suggestion_scoring import value_languages

from .errors import ApplyRefusedError

_PUSHABLE: tuple[SuggestionStatus, ...] = (SuggestionStatus.ACCEPTED, SuggestionStatus.EDITED)

_UPDATE_OPS: frozenset[str] = UPDATE_OPS

_CREATE_OPS: frozenset[str] = CREATE_OPS

EntitySelection = list[tuple[str, str | None]] | None

ProgressCallback = Any

_DATE_PROPERTY_TYPES: frozenset[str] = frozenset({"date", "multidate", "daterange", "multidaterange"})

_RELATIONSHIP_PROPERTY_TYPE = RELATIONSHIP_TYPE

_GEOLOCATION_PROPERTY_TYPE = "geolocation"

_MAX_ERROR_CHARS = 400


def accepted_value(value: Any, rejected: list[str]) -> Any:
    """The proposal minus the operator's rejected labels (pure).

    Untouched unless ``value`` is a non-empty list and ``rejected`` is
    non-empty; only str entries are ever filtered, so dates/links/relationship
    lists cannot be altered.
    """
    if not isinstance(value, (list, tuple)) or not value or not rejected:
        return value
    dropped = set(rejected)
    if not any(isinstance(entry, str) for entry in value):
        return value
    return [entry for entry in value if not (isinstance(entry, str) and entry in dropped)]


def effective_value(suggestion: Suggestion) -> object:
    """The value a Run writes for one row: the edit when present, else the proposal.

    An ``EDITED`` row always uses ``edited_value``; every other pushable row
    uses its proposal MINUS the multiselect labels the operator rejected.
    """
    if suggestion.status == SuggestionStatus.EDITED and suggestion.edited_value is not None:
        return _decode_edited(suggestion.edited_value)
    return accepted_value(suggestion.proposed_value, suggestion.rejected_values)


def select_labels(suggestion: Suggestion) -> list[str]:
    """The labels a select/multiselect Run writes: edit > explicit selection > proposal.

    An ``EDITED`` row writes its decoded edit; a row carrying the operator's
    explicit ``selected_values`` writes exactly those (an empty list writes
    nothing — but such a row is sentinel-discarded and never pushed); every
    other row writes its proposal minus the rejected labels. ``labels_of``
    normalizes all three shapes, so the thesaurus lookup downstream sees plain
    label strings.
    """
    if suggestion.status == SuggestionStatus.EDITED and suggestion.edited_value is not None:
        return labels_of(_decode_edited(suggestion.edited_value))
    if suggestion.selected_values is not None:
        return labels_of(suggestion.selected_values)
    return labels_of(accepted_value(suggestion.proposed_value, suggestion.rejected_values))


def relationship_targets(suggestion: Suggestion) -> list[str]:
    """The related sharedIds a relationship Run writes (pure).

    An ``EDITED`` row writes its decoded edit; otherwise the operator's explicit
    ``selected_values`` wins (an empty list writes nothing — but such a row is
    sentinel-discarded and never pushed); a plain row writes the proposal minus
    the rejected targets.
    """
    if suggestion.status == SuggestionStatus.EDITED and suggestion.edited_value is not None:
        return relationship_selection(_decode_edited(suggestion.edited_value), suggestion.rejected_values, None)
    return relationship_selection(
        suggestion.proposed_value,
        suggestion.rejected_values,
        suggestion.selected_values,
    )


def _decode_edited(text: str) -> object:
    """Best-effort decode of an operator-typed value (JSON when it parses, else the string)."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text


def _selected(suggestion: Suggestion, selection: EntitySelection | None) -> bool:
    """Whether a row belongs to the Run's selection (pure).

    The selection is the table's checked (sharedId, language) rows. ``None``
    means "the whole table" (a CLI run); a row is in it when its exact
    (sharedId, language) pair is listed — which is what lets the operator run
    the ``es`` row of a translation task WITHOUT touching its ``en`` sibling.
    """
    if selection is None:
        return True
    return (suggestion.shared_id, suggestion.language) in selection


def _in_rows(suggestion: Suggestion, row_ids: list[int] | None) -> bool:
    """Whether a row is one of the checked SUBOPERATIONS (pure).

    The review table's checkboxes are per SUBOPERATION (one suggestion row), so
    a Run of a subset of one entity-language group must narrow below the pair.
    ``None`` means "no row-level constraint" — the pair selection (or the whole
    table) stands, so every existing caller keeps its behaviour. A row with no
    id yet (a freshly built, unpersisted suggestion) can never be in an explicit
    id list.
    """
    if row_ids is None:
        return True
    return suggestion.id is not None and suggestion.id in row_ids


def language_scoped_value(value: Any, language: str | None) -> Any:
    """One property's value reduced to the ONE entry Uwazi stores (pure).

    Uwazi keeps a text/richtext property as a single ``{"value": ...}`` entry on
    the entity-language ROW the write targets (the ``locale`` cookie picks the
    row), and the property validator caps that array at ``max(1)`` — a second
    entry is rejected with *"Text Property only accepts a single value."* So a
    per-language value list is narrowed to the entry of the row's own language;
    the entity's OTHER languages are other ROWS, and this write never touches
    them. That is the opposite of merging them back in, which produced exactly
    the over-long array Uwazi refused.

    A row that names no language, or names one the value does not carry, keeps
    the value's first entry: the row is what Uwazi is being asked to store, and
    one entry is all it accepts. A language-INDEPENDENT value (a select label, a
    date, a link dict, a relationship list) carries no ``language`` key at all
    and passes through untouched — only a plain string is stamped with the row's
    language (:func:`stamp_language`).
    """
    if not value_languages(value):
        return stamp_language(value, language)
    entries = [entry for entry in value if isinstance(entry, dict)]
    matching = [entry for entry in entries if language is not None and entry.get("language") == language]
    chosen = matching or entries
    return [chosen[0]] if chosen else value


def _value_entries(value: Any) -> list[Any]:
    """Wrap every scalar of a value in Uwazi's ``{"value": ...}`` envelope (pure).

    A value already carrying an envelope entry is left alone; a date list is a
    list of NUMBERS, which are still wrapped because the on-disk shape is
    ``[{"value": <timestamp>}]`` (the same shape the mapper and the raw read
    use). ``{"from", "to"}`` range dicts are wrapped as one entry.
    """
    items = list(value) if isinstance(value, (list, tuple)) else [value]
    return [item if isinstance(item, dict) and "value" in item else {"value": item} for item in items]


def _relationship_entries(value: Any) -> list[dict[str, str]]:
    """A relationship value as Uwazi's ``[{"value": <sharedId>}]`` (pure).

    The write contract hands the agent related entities by ``shared_id`` (a
    string or a list of them); the raw metadata row stores the id in a
    ``{"value": ...}`` entry, exactly like the mapper's coercion. A read-shape
    entry (``{"shared_id", "title"}``) is accepted when an operator echoes it
    back, but only the id is written.
    """
    items = list(value) if isinstance(value, (list, tuple)) else [value]
    entries: list[dict[str, str]] = []
    for item in items:
        target = item
        if isinstance(item, dict):
            target = item.get("shared_id") or item.get("sharedId") or item.get("value")
        if isinstance(target, str) and target.strip():
            entries.append({"value": target.strip()})
    return entries


def _geolocation_entries(value: Any) -> list[dict[str, Any]]:
    """A geolocation value as Uwazi's ``[{"value": {"lat", "lon", "label"}}]`` (pure).

    Accepts the write contract's ``[lat, lon]`` pair, a ``{"lat", "lon"}`` dict,
    and the on-disk envelope (or a list of them) an operator may echo back.
    Anything unrecognised is left for Uwazi to refuse rather than being
    silently mangled.
    """

    def entry(mapping: dict[str, Any]) -> dict[str, Any] | None:
        if "lat" not in mapping or "lon" not in mapping:
            return None
        return {
            "value": {
                "lat": float(mapping["lat"]),
                "lon": float(mapping["lon"]),
                "label": str(mapping.get("label", "")),
            }
        }

    if isinstance(value, (list, tuple)) and len(value) >= 2 and not isinstance(value[0], (dict, list, tuple)):
        try:
            return [{"value": {"lat": float(value[0]), "lon": float(value[1]), "label": ""}}]
        except (TypeError, ValueError):
            pass

    items = list(value) if isinstance(value, (list, tuple)) else [value]
    entries: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, dict):
            inner = item.get("value") if isinstance(item.get("value"), dict) else item
            found = entry(inner)
            if found is not None:
                entries.append(found)
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            try:
                entries.append({"value": {"lat": float(item[0]), "lon": float(item[1]), "label": ""}})
            except (TypeError, ValueError):
                continue
    return entries or _value_entries(value)


def metadata_value(value: Any, language: str | None, property_type: str | None = None) -> Any:
    """The value Uwazi actually stores for one metadata KEY (pure).

    Every metadata key is an ARRAY of ``{"value": ...}`` entries in Uwazi's
    schema: a select is ``[{"value": <uuid>}]``, a date
    ``[{"value": <timestamp>}]``, a text ``[{"value", "language"}]``. A recorded
    ``None`` means "clear this value" (the planner's way to empty a date), and
    sending the literal ``null`` is refused — ``422 Expected array, received
    null`` on the per-entity write, and a 500 (which the HTTP layer then retried
    for minutes) on the bulk endpoint. An EMPTY ARRAY is how the same clear is
    expressed, so that is what this boundary sends.

    A recorded SCALAR is wrapped in the same envelope for the same reason: the
    raw partial-update path bypasses the entity mapper's coercion, so a bare
    ``"A/HRC/37/NGO/92 ES"`` (or a link dict, or a number) reaches Uwazi as-is
    and is refused with ``422 Expected array, received string``. Per-language
    values are already a ``[{"value", "language"}]`` list from
    :func:`stamp_language`; a language-independent value keeps its shape and is
    wrapped as ``[{"value": ...}]`` exactly as the mapper would have done.

    Date-family values are special: their list is ALREADY an array but holds
    raw numbers/range dicts, so each entry still needs the envelope — the
    on-disk row is ``[{"value": <timestamp>}]``, not ``[<timestamp>]``.
    """
    scoped = language_scoped_value(value, language)
    if scoped is None:
        return []
    if property_type in _DATE_PROPERTY_TYPES:
        return _value_entries(scoped)
    if property_type == _RELATIONSHIP_PROPERTY_TYPE:
        return _relationship_entries(scoped)
    if property_type == _GEOLOCATION_PROPERTY_TYPE:
        return _geolocation_entries(scoped)
    if isinstance(scoped, tuple):
        return list(scoped)
    if isinstance(scoped, list):
        return scoped
    if isinstance(scoped, dict) and "value" in scoped:
        return [scoped]
    return [{"value": scoped}]


def stamp_language(value: Any, language: str | None) -> Any:
    """Tag a bare text value with the language the row writes it in (pure).

    A row's language is the write's target language, so it has to REACH the value
    Uwazi stores: Uwazi keeps text/richtext as a list of ``{"value",
    "language"}`` entries and replaces a property's whole list on update, so a
    bare ``"A/HRC/37/NGO/92 ES"`` written as-is lands as a language-AGNOSTIC
    value — the Spanish row would overwrite the property instead of joining it,
    and it would not read back as the Spanish text the operator approved.

    Only a plain string is stamped. A row with NO language still gets the
    required array envelope (``[{"value": "..."}]``) — the raw update path does
    not run the entity mapper, so leaving the bare string made Uwazi refuse the
    write with ``422 Expected array, received string``. A value that already
    carries its own languages keeps them (they are the more specific
    statement), and a language-independent value (a select label, a date, a
    link) is left exactly as the script wrote it. A stray ``language`` on such a
    property is dropped by the write adapter's coercion, not by guessing here.
    """
    if not isinstance(value, str):
        return value
    entry: dict[str, Any] = {"value": value}
    if language is not None:
        entry["language"] = language
    return [entry]


def write_failures(results: Any, source_ids: list[str] | None = None) -> list[dict[str, str]]:
    """The REFUSED writes in one CRUD helper's return value, as ``{shared_id, error}`` (pure).

    The write helpers do not raise on a refused entity: the entity API catches
    the upload error and returns ``success=False`` with the message, so a caller
    that ignores the return value reports success over an entity Uwazi never
    changed. This is the read that makes the failure visible.

    Both return shapes are understood: the per-entity list ``create_entities`` /
    ``update_entities`` / ``delete_entities`` / ``set_publish_status`` /
    ``create_relationships`` return, and the aggregate summary dict
    ``publish_entities`` / ``unpublish_entities`` return. An entry without a
    ``success`` key is not a failure — only an explicit falsy one is.

    ``source_ids`` is the payload's own identity list (a create's
    ``dry-created-N`` placeholder, which the per-entity result CANNOT echo
    because Uwazi only mints a sharedId on success), aligned with ``results`` so
    a refused create is still attributable to the row that produced it.
    """
    if isinstance(results, dict):
        errors = [_failure_entry(entry) for entry in (results.get("errors") or []) if isinstance(entry, dict)]
        if errors:
            return errors
        count = _as_int(results.get("failure_count"))
        return [{"shared_id": "", "error": f"{count} entity/entities could not be updated"}] if count else []
    if not isinstance(results, list):
        return []
    failures: list[dict[str, str]] = []
    for index, entry in enumerate(results):
        if not isinstance(entry, dict) or entry.get("success", True):
            continue
        failure = _failure_entry(entry)
        if not failure["shared_id"] and source_ids is not None and index < len(source_ids):
            failure["shared_id"] = source_ids[index]
        failures.append(failure)
    return failures


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _failure_entry(entry: dict[str, Any]) -> dict[str, str]:
    """One refused write as ``{shared_id, error}``, with the error trimmed (pure)."""
    error = str(entry.get("error") or entry.get("error_code") or "write refused by Uwazi")
    return {
        "shared_id": str(entry.get("shared_id") or entry.get("id") or ""),
        "error": error if len(error) <= _MAX_ERROR_CHARS else error[:_MAX_ERROR_CHARS] + "…",
    }


def track_progress(progress: ProgressCallback | None, units_so_far: list[int]) -> ProgressCallback:
    """Wrap a progress callback while recording the latest fraction (pure).

    The wrapper is what :meth:`_write` installs as its ``progress`` for the rest
    of the pass. It MUST call the ORIGINAL callback, never the name it is
    installed under: the previous version closed over ``progress`` after that
    name was rebound to the wrapper itself, so every tick (and every gate note)
    recursed until ``maximum recursion depth exceeded`` — the operator's real
    progress callback was never called.

    ``units_so_far`` is the one-element list the gate-note publisher reads so a
    note carries the live fraction rather than resetting it.
    """

    def _tracked(done: int, total: int, note: str | None = None) -> None:
        units_so_far[0] = done
        if progress is None:
            return
        if note is None:
            progress(done, total)  # a fraction tick keeps the current phase note
        else:
            progress(done, total, note)

    return _tracked


def language_groups(payloads: list[dict], default_language: str) -> list[tuple[str, list[dict]]]:
    """Payloads regrouped by the entity-language ROW each one writes (pure).

    Uwazi selects the row through the ``locale`` cookie of the write, so a Run
    that writes both the ``es`` and the ``en`` row of one entity must issue one
    call PER LANGUAGE: a single call would carry the Spanish text onto the
    English row. A payload with no language (a select change, a title) falls back
    to the run's default, exactly like the CRUD helpers do — and every payload in
    the group is stamped with the concrete language, because the entity models
    declare ``language`` as a plain string, not an optional one.
    """
    order: list[str] = []
    grouped: dict[str, list[dict]] = {}
    for payload in payloads:
        language = str(payload.get("language") or default_language)
        if language not in grouped:
            grouped[language] = []
            order.append(language)
        grouped[language].append({**payload, "language": language})
    return [(language, grouped[language]) for language in order]


def pushable_rows(
    suggestions: list[Suggestion],
    selection: EntitySelection | None = None,
    row_ids: list[int] | None = None,
) -> list[Suggestion]:
    """The rows a Run would push, in order (pure).

    Only ``ACCEPTED``/``EDITED`` rows of executable ops (``update`` rows need a
    property name; ``create``/``delete``/``publish``/``unpublish``/
    ``set_publish_status``/``create_relationships`` rows are whole-entity; the
    merge ops behave as their target's op, with the source deletes carried in
    ``merge_source_ids``). ``selection`` restricts to the checked (sharedId,
    language) rows when given; ``None`` means the whole set. ``row_ids`` narrows
    further to the checked SUBOPERATIONS; ``None`` means the pair selection (or
    the whole set) stands.
    """
    rows: list[Suggestion] = []
    for s in suggestions:
        if s.op in DISPLAY_ONLY_OPS:
            # Display-only rows are never written to Uwazi.
            continue
        if s.is_validation:
            # A holdout row is SCORE EVIDENCE: it measures the script, it is not
            # work. The UI hides its Run, and this guard is what makes that a
            # property of the system rather than of one screen.
            continue
        if s.status not in _PUSHABLE:
            continue
        if not _selected(s, selection):
            continue
        if not _in_rows(s, row_ids):
            continue
        if s.op == "update" and s.property_name is None:
            continue
        rows.append(s)
    return rows


def partition_for_apply(
    rows: list[Suggestion],
    selection: EntitySelection | None = None,
    row_ids: list[int] | None = None,
) -> tuple[list[Suggestion], list[Suggestion]]:
    """(property_rows, whole_entity_rows) among pushable rows for the given subset (pure).

    ``property_rows`` = ACCEPTED/EDITED ``update``/``update_and_delete`` rows
    with a ``property_name`` — the partial-update path (``build_apply_updates``
    groups them per entity). ``whole_entity_rows`` = everything else pushable:
    rows of the whole-entity ops (delete/publish/unpublish/set_publish_status/
    create_relationships), propertyless update rows, and all
    ``create``/``create_and_delete`` rows (a create's sharedId is the
    ``dry-created-N`` placeholder until Run mints a real one).

    A propertyless MERGE row is the all-no-op anchor: it writes no payload of
    its own (its sources are handled by :func:`merge_deletions`), so it belongs
    to neither path and is not counted as a write unit. The same holds for a
    standalone ``move_files`` row: it adds files, not entity values.
    """
    property_rows: list[Suggestion] = []
    whole_entity_rows: list[Suggestion] = []
    for suggestion in rows:
        if suggestion.op in DISPLAY_ONLY_OPS:
            # Display-only rows are never written to Uwazi.
            continue
        if suggestion.is_validation:
            # Holdout evidence is never work (see pushable_rows).
            continue
        if suggestion.status not in _PUSHABLE:
            continue
        if not _selected(suggestion, selection):
            continue
        if not _in_rows(suggestion, row_ids):
            continue
        if (suggestion.op in MERGE_OPS or suggestion.op in FILE_OPS) and suggestion.property_name is None:
            # The merge anchor exists only to carry merge_source_ids; the
            # target writes are the merge's property rows (if any), the
            # sources are deleted from `merges`, and a file copy is executed
            # from `file_moves`. Counting either here would add a write unit
            # that nothing ever writes.
            continue
        if suggestion.op in _UPDATE_OPS and suggestion.property_name is not None:
            property_rows.append(suggestion)
        else:
            whole_entity_rows.append(suggestion)
    return property_rows, whole_entity_rows


def merge_deletions(rows: list[Suggestion]) -> list[tuple[str, list[str]]]:
    """The merge targets and the source ids each Run deletes, in first-appearance order (pure).

    Every merge row of one target carries the same ``merge_source_ids`` (the
    target's property rows each name them so any subset of rows still describes
    the whole action), so the pairs are deduped by target and the source order
    is kept. A non-merge row, or a merge row without sources, contributes
    nothing.
    """
    order: list[str] = []
    sources_by_target: dict[str, list[str]] = {}
    for row in rows:
        if row.op not in ("update_and_delete", "create_and_delete") or not row.merge_source_ids:
            continue
        if row.shared_id not in sources_by_target:
            sources_by_target[row.shared_id] = []
            order.append(row.shared_id)
        for source in row.merge_source_ids:
            if source and source not in sources_by_target[row.shared_id]:
                sources_by_target[row.shared_id].append(source)
    return [(target, sources_by_target[target]) for target in order]


def file_move_targets(rows: list[Suggestion]) -> list[tuple[str, list[str]]]:
    """The standalone file copies a Run performs, in first-appearance order (pure).

    A ``move_files`` row is the review form of a scripted
    ``move_files_to_entity`` that is NOT a merge: the target gains the sources'
    uploaded files and the sources are left untouched. Each row's
    ``shared_id`` is the target and its ``merge_source_ids`` the sources; rows
    for the same target are deduped (their sources merged) so one target is one
    move.
    """
    order: list[str] = []
    sources_by_target: dict[str, list[str]] = {}
    for row in rows:
        if row.op != MOVE_OP or not row.shared_id:
            continue
        if not row.merge_source_ids:
            continue
        if row.shared_id not in sources_by_target:
            sources_by_target[row.shared_id] = []
            order.append(row.shared_id)
        for source in row.merge_source_ids:
            if source and source != row.shared_id and source not in sources_by_target[row.shared_id]:
                sources_by_target[row.shared_id].append(source)
    return [(target, sources_by_target[target]) for target in order if sources_by_target[target]]


def payload_units(payload: dict) -> int:
    """Suboperations one write payload carries (pure).

    An update/create payload groups one property row per metadata entry plus an
    optional title row — exactly the review table's suboperations. A payload
    with neither (never seen, but guarded) still counts as ONE write so the
    progress fraction keeps moving.

    This is the ONLY place the Run's progress numerator and denominator agree:
    :func:`apply_units_total` sums it for the total and :meth:`_write` sums it
    per batch, so "done" can never outgrow "total" by construction.
    """
    units = len(payload.get("metadata") or {}) + (1 if "title" in payload else 0)
    return units if units > 0 else 1


def apply_units_total(
    updates: list[dict],
    creates: list[dict],
    whole_entity_rows: list[Suggestion],
    merges: list[tuple[str, list[str]]] | None = None,
    file_moves: list[tuple[str, list[str]]] | None = None,
) -> int:
    """Suboperations a Run will write, counting from its payloads (pure).

    Property rows (grouped into ``updates``/``creates`` payloads), each
    whole-entity row (delete/publish/unpublish/set_publish_status/relationship
    — one unit each), each merge's source deletions, and each standalone file
    copy (one unit per target). This is the Run progress toast's denominator
    ("13/1230 suboperations").
    """
    total = sum(payload_units(payload) for payload in (*updates, *creates))
    total += len(whole_entity_rows)
    total += sum(len(sources) for _target, sources in (merges or []))
    total += len(file_moves or [])
    return total


def merge_moves(
    merges: list[tuple[str, list[str]]],
    failed_targets: set[str] | None = None,
    created_ids: dict[str, str] | None = None,
) -> list[tuple[str, list[str]]]:
    """The file moves a Run performs for its merges/file copies, in order (pure).

    Each ``(target, sources)`` from :func:`merge_deletions` or
    :func:`file_move_targets` becomes the ``(real_target, sources)`` the file
    mover needs:

    - a merge target written by a CREATE is still keyed by the ``dry-created-N``
      placeholder its rows carry (a create has no real sharedId until Run), so
      ``created_ids`` resolves it to the sharedId Uwazi minted — without this
      the mover would fetch a target that does not exist;
    - a target whose own write was REFUSED is dropped: the merge never
      happened, so nothing is moved (and :func:`merge_deletions` likewise skips
      its source deletes).

    A merge with no sources contributes no move.
    """
    failed = failed_targets or set()
    resolved = created_ids or {}
    moves: list[tuple[str, list[str]]] = []
    for target, sources in merges:
        if not sources or target in failed:
            continue
        moves.append((resolved.get(target, target), list(sources)))
    return moves


def build_apply_updates(
    suggestions: list[Suggestion],
    selection: EntitySelection | None = None,
    row_ids: list[int] | None = None,
    property_types: dict[str, str] | None = None,
    select_value_ids: dict[tuple[str, str], dict[str, str]] | None = None,
) -> list[dict]:
    """Group pushable property suggestions into per-entity-LANGUAGE update payloads (pure).

    Rows group by (sharedId, language) into ONE ``{"shared_id", "language",
    "metadata": {...}}`` payload each (plus ``"title"`` when a ``_title`` row is
    present, and ``"template_name"`` when the recording carried one); an
    ``EDITED`` row overrides its proposal. Group and key order follow first
    appearance, so a batch is deterministic.

    Only ``update``/``update_and_delete`` rows with a property name enter this
    grouping — the ops whose sharedId already identifies a real entity.
    ``create``/``create_and_delete`` rows are property rows
    too, but their sharedId is the ``dry-created-N`` placeholder, so they are
    grouped by :func:`build_apply_creates` (which needs no existing entity to
    read) and written through ``create_entities``.

    The language is part of the key because it selects the entity-language ROW
    the write lands on (Uwazi picks the row through the write's ``locale``
    cookie). Two checked rows for the same property in different languages are
    therefore two payloads written to two rows — NOT one payload carrying a
    merged value list, which Uwazi refuses because a text property holds a
    single value per row.

    Every value is narrowed to its own language's entry by
    :func:`language_scoped_value` before it enters the payload.
    """
    grouped: dict[tuple[str, str | None], dict] = {}
    order: list[tuple[str, str | None]] = []
    for suggestion in pushable_rows(suggestions, selection, row_ids):
        if suggestion.op not in _UPDATE_OPS or suggestion.property_name is None:
            continue
        key = (suggestion.shared_id, suggestion.language)
        if key not in grouped:
            grouped[key] = {"metadata": {}, "title": None, "template_name": None}
            order.append(key)
        payload = grouped[key]
        value = effective_value(suggestion)
        if suggestion.property_name == TITLE_PROPERTY:
            payload["title"] = value
        else:
            # DATE-typed values are coerced to Uwazi's shape here, at the
            # payload boundary — this metadata is POSTed raw by the
            # property-scoped write path (see this docstring).
            prop_type = (property_types or {}).get(suggestion.property_name or "")
            if prop_type in SELECT_TYPES:
                # Uwazi's raw update path resolves select values by THESAURUS
                # ID (``getValueById``): a label either fails validation or is
                # silently dropped, so the accepted labels must become the
                # thesaurus value UUIDs here.
                key = (suggestion.template_name or "", suggestion.property_name or "")
                ids_by_label = (select_value_ids or {}).get(key)
                if not ids_by_label:
                    raise ApplyRefusedError(
                        f"cannot write {suggestion.property_name!r} of {suggestion.shared_id!r}: its thesaurus "
                        "values could not be resolved on the instance, so the labels cannot become thesaurus ids"
                    )
                try:
                    payload["metadata"][suggestion.property_name] = select_value_envelope(
                        single_value(prop_type, select_labels(suggestion)),
                        ids_by_label,
                        suggestion.property_name,
                    )
                except UnknownThesaurusValue as exc:
                    raise ApplyRefusedError(str(exc)) from exc
            elif prop_type == RELATIONSHIP_TYPE:
                # A relationship is always MULTI-valued, and its value is the
                # set of target sharedIds (``[{"value": <sharedId>}]``). The
                # operator's explicit selection — or the proposal minus the
                # rejected targets — is what the Run writes.
                payload["metadata"][suggestion.property_name] = _relationship_entries(relationship_targets(suggestion))
            else:
                raw_value = coerce_date_property_value(value, prop_type if is_date_property_type(prop_type) else None)
                payload["metadata"][suggestion.property_name] = metadata_value(raw_value, suggestion.language, prop_type)
        if suggestion.template_name and not payload["template_name"]:
            payload["template_name"] = suggestion.template_name
    updates: list[dict] = []
    for key in order:
        shared_id, language = key
        payload = grouped[key]
        entry: dict = {"shared_id": shared_id, "language": language, "metadata": payload["metadata"]}
        if payload["title"] is not None:
            entry["title"] = payload["title"]
        if payload["template_name"]:
            entry["template_name"] = payload["template_name"]
        updates.append(entry)
    return updates


def build_apply_creates(
    suggestions: list[Suggestion],
    selection: EntitySelection | None = None,
    row_ids: list[int] | None = None,
    property_types: dict[str, str] | None = None,
) -> list[dict]:
    """Group pushable create suggestions into per-entity-LANGUAGE create payloads (pure).

    A create's rows carry the same expansion an update does: one suggestion per
    metadata key + one ``_title`` row + the template per row. The FIRST row's
    ``template_name`` wins for the payload (a create is one template by
    definition); titles and metadata group exactly like
    :func:`build_apply_updates`, INCLUDING the language: rows are grouped per
    (placeholder sharedId, language) and carry ``"language"``, so a create
    recorded with several languages is written as one entity row per language
    rather than one row holding a merged value list Uwazi would refuse.

    ``"source_shared_id"`` keeps the ``dry-created-N`` placeholder the grouping
    keyed on: the create result cannot echo it (Uwazi only mints a sharedId on
    success), so it is what makes a refused create attributable to its rows.

    Rows whose ``template_name`` is missing raise :class:`ApplyRefusedError` — a
    create cannot be written without a template.

    ``property_types`` (optional) makes a relationship row write the operator's
    explicit target selection — or the proposal minus the rejected targets —
    exactly like an update. Without it the recorded value is written as-is,
    which is what a create through the entity mapper expects (bare shared ids).
    """
    grouped: dict[tuple[str, str | None], dict] = {}
    order: list[tuple[str, str | None]] = []
    for suggestion in pushable_rows(suggestions, selection, row_ids):
        if suggestion.op not in _CREATE_OPS:
            continue
        key = (suggestion.shared_id, suggestion.language)
        if key not in grouped:
            grouped[key] = {"metadata": {}, "title": None, "template_name": None}
            order.append(key)
        payload = grouped[key]
        value = effective_value(suggestion)
        prop_type = (property_types or {}).get(suggestion.property_name or "")
        if suggestion.property_name == TITLE_PROPERTY:
            payload["title"] = value
        elif suggestion.property_name is not None and prop_type == RELATIONSHIP_TYPE:
            payload["metadata"][suggestion.property_name] = relationship_targets(suggestion)
        elif suggestion.property_name is not None:
            payload["metadata"][suggestion.property_name] = metadata_value(value, suggestion.language)
        if suggestion.template_name and not payload["template_name"]:
            payload["template_name"] = suggestion.template_name
    creates: list[dict] = []
    for order_index, key in enumerate(order):
        shared_id, language = key
        payload = grouped[key]
        if not payload["template_name"]:
            raise ApplyRefusedError(f"create row {payload['title']!r} carries no template name; regenerate the task")
        creates.append(
            {
                # Uwazi refuses a create whose title is EMPTY (``POST
                # /api/entities``: "String must contain at least 1 character(s)"),
                # and one placeholder-titleless row refused the whole Run.
                # Only the entity's own identity is known here, so the fallback
                # names what the row IS and stays editable in Uwazi afterwards:
                # "<template> <source>" (and a 1-based position when the
                # grouping carried no source id).
                "title": payload["title"] or create_fallback_title(payload["template_name"], shared_id, order_index),
                "template_name": payload["template_name"],
                "metadata": payload["metadata"],
                "language": language,
                "source_shared_id": shared_id,
            }
        )
    return creates


def create_fallback_title(template_name: str, source_id: str, position: int) -> str:
    """The title a create with no title row is written under (pure).

    "<Template> <source>" reads as what the entity is and where it came from
    (the review table shows the same ids), and the 1-based position keeps two
    placeholders from colliding when the record carried no id.
    """
    template = str(template_name or "").strip() or "Entity"
    source = str(source_id or "").strip()
    base = f"{template} {source}".strip() if source else ""
    return base if base else f"{template} {position + 1}"


def property_scope_ids(
    updates: list[dict],
    whole_entity_rows: list[Suggestion],
    merges: list[tuple[str, list[str]]] | None = None,
) -> set[str]:
    """The entities whose ONLY writes in this Run are property updates (pure).

    Those are the ones a property-scoped snapshot is CORRECT for: nothing else
    in the Run can touch a field outside the touched property set, so backing up
    just those keys is enough to undo the write — and it is ~30× smaller than a
    whole-entity snapshot.

    Everything else keeps the whole-entity snapshot, because those writes change
    parts of the entity a property restore cannot reach: a delete (the row is
    gone), a publish/unpublish or permission change (a separate collection), a
    merge's target (its sources' files move onto it) and its sources (they are
    deleted), and BOTH endpoints of a relationship row (Uwazi denormalizes the
    hub onto each entity's raw).
    """
    touched_elsewhere: set[str] = set()
    for row in whole_entity_rows:
        if row.op == "create_relationships":
            rel = row.proposed_value if isinstance(row.proposed_value, dict) else {}
            for key in ("from_entity_shared_id", "to_entity_shared_id"):
                value = rel.get(key)
                if isinstance(value, str) and value:
                    touched_elsewhere.add(value)
            continue
        if row.shared_id:
            touched_elsewhere.add(row.shared_id)
    for target, sources in merges or []:
        touched_elsewhere.add(target)
        touched_elsewhere.update(sources)
    return {payload["shared_id"] for payload in updates} - touched_elsewhere
