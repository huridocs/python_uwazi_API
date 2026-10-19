"""Execute reviewed table rows onto Uwazi as ONE revertable apply run.

The review UI separates *deciding* from *writing*: the table's rows are
precomputed by the planning pass (the recorded dry-run writes), and the
operator's Run action executes them with this module's DETERMINISTIC HEURISTIC
— no script runs, no LLM calls, no re-computation. Per row, the heuristic is:

- ``update`` rows with a property (or ``_title``): group per
  (sharedId, LANGUAGE) into one partial-update payload each
  (``build_apply_updates``, pure);
- ``create`` rows: group per (placeholder sharedId, language) into
  ``AgentEntityCreate`` payloads and call ``create_entities`` through the
  intercept;
- ``delete`` rows: ``delete_entities`` through the intercept;
- ``publish``/``unpublish``/``set_publish_status`` rows: ``set_publish_status``
  through the intercept;
- ``create_relationships`` rows: ``create_relationships`` through the intercept.

Every write goes through :class:`BackupIntercept`, so each affected entity is
snapshotted BEFORE it changes — and those snapshots accumulate on the RUN'S OWN
manifest. A Run therefore adds NO row to the tasks table: it only changes the
suggestion rows' status and moves the run itself to ``EXECUTED``, so one
whole-task revert undoes it (with every other write the run made).
``build_apply_updates`` (pure) is the unit-test target for the payload grouping.

Language is part of a write's identity. Uwazi stores one entity-language ROW per
translation (the ``locale`` cookie picks the row) and a text/richtext property
holds exactly ONE value entry on that row — the property validator caps it at
``max(1)`` and rejects a second entry with *"Text Property only accepts a single
value"*. So a Run writes ONE payload per (entity, language) and never merges an
entity's other languages back into a value list: the other languages are other
ROWS, untouched by this write.

Failures are RAISED, not ignored. The entity API reports a refused write as
``success=False`` in the helper's return value rather than by raising, so a Run
that read those results carelessly reported success over an untouched entity
(a Uwazi 422 and an ``outcome=success`` audit record). :func:`write_failures`
reads the per-entity results and :class:`ApplyWriteError` surfaces them: only the
writes that landed become ``APPLIED``, and the rows whose entity was refused keep
their ``ACCEPTED`` status so they can be run again.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from uwazi_admin_agent.configuration import EXECUTE_BATCH_SIZE, MAX_ENTITIES_PER_RUN
from uwazi_admin_agent.domain.manifest import MigrationManifest, RunStatus
from uwazi_admin_agent.domain.suggestion import TITLE_PROPERTY, Suggestion, SuggestionFilter, SuggestionStatus
from uwazi_admin_agent.domain.suggestion_scoring import value_languages
from uwazi_admin_agent.ports.audit_log_port import AuditLogPort
from uwazi_admin_agent.ports.backup_store_port import BackupStorePort
from uwazi_admin_agent.ports.cache_invalidation_port import CacheInvalidationPort
from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort
from uwazi_admin_agent.ports.suggestion_store_port import SuggestionStorePort
from uwazi_admin_agent.use_cases.backup_intercept import BackupIntercept
from uwazi_admin_agent.use_cases.script_exec_namespace import _build_sync_crud_functions
from uwazi_agent.ports.entity_api_port import EntityApiPort
from uwazi_agent.ports.relationship_api_port import RelationshipApiPort
from uwazi_agent.use_cases.tools.tool_call_cache import ToolCallCache

# The statuses a Run pushes: an accepted proposal, or an operator-edited one.
_PUSHABLE: tuple[SuggestionStatus, ...] = (SuggestionStatus.ACCEPTED, SuggestionStatus.EDITED)

# The op whose per-property rows the PARTIAL-UPDATE path groups. A ``create``
# also expands to property rows when recorded, but its rows carry the
# ``dry-created-N`` placeholder sharedId — an id Uwazi has never seen — so they
# are grouped by :func:`build_apply_creates` and written as a create. Letting one
# reach the update path made :meth:`ApplySuggestionsUseCase._enrich` read a raw
# that cannot exist (a 404 by design) and abort the whole Run with a raw
# ``get_raw_by_shared_id(dry-created-0) failed (404)``.
_UPDATE_OP: str = "update"

# What a Run acts on: the checked (sharedId, language) rows of the review
# table. ``None`` (the CLI path) means the whole attempt.
EntitySelection = list[tuple[str, str | None]] | None


class ApplyRefusedError(RuntimeError):
    """The Run was refused (nothing pushable among the selected rows)."""


class ApplyWriteError(RuntimeError):
    """Uwazi refused at least one write of the Run.

    Distinct from :class:`ApplyRefusedError`, which means nothing was even
    attempted: here the Run DID execute and every write that could land landed
    (revertable, like any other write), but the CRUD helpers report a refused
    upload as ``success=False`` in their return value instead of raising. Reading
    those results is what keeps a Uwazi 422 from being reported to the operator
    as a completed Run, so the failures are raised here instead.
    """

    def __init__(self, failures: list[dict[str, str]]) -> None:
        self.failures: list[dict[str, str]] = failures
        super().__init__(format_write_failures(failures))


def effective_value(suggestion: Suggestion) -> object:
    """The value a Run writes for one row: the edit when present, else the proposal.

    An ``EDITED`` row always uses ``edited_value``; every other pushable row uses
    its proposal.
    """
    if suggestion.status == SuggestionStatus.EDITED and suggestion.edited_value is not None:
        return _decode_edited(suggestion.edited_value)
    return suggestion.proposed_value


def _decode_edited(text: str) -> object:
    """Best-effort decode of an operator-typed value (JSON when it parses, else the string)."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text


# NOTE: no value re-shaping happens here. The recorded payload is already in the
# exact shape the script contract mandated (a select is its LABEL string, a
# multiselect a list of labels, a link a dict, ...), and the entity mapper
# coerces those shapes by property TYPE. Re-wrapping a proposal into
# ``[{"value": ...}]`` would hand the mapper a list where a select expects a
# scalar and it rejects it — so the reviewed value is applied verbatim.


def _selected(suggestion: Suggestion, selection: EntitySelection | None) -> bool:
    """Whether a row belongs to the Run's selection (pure).

    The selection is the table's checked (sharedId, language) rows. ``None``
    means "the whole attempt" (a CLI run); a row is in it when its exact
    (sharedId, language) pair is listed — which is what lets the operator run
    the ``es`` row of a translation task WITHOUT touching its ``en`` sibling.
    """
    if selection is None:
        return True
    return (suggestion.shared_id, suggestion.language) in selection


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


def stamp_language(value: Any, language: str | None) -> Any:
    """Tag a bare text value with the language the row writes it in (pure).

    A row's language is the write's target language, so it has to REACH the value
    Uwazi stores: Uwazi keeps text/richtext as a list of ``{"value",
    "language"}`` entries and replaces a property's whole list on update, so a
    bare ``"A/HRC/37/NGO/92 ES"`` written as-is lands as a language-AGNOSTIC
    value — the Spanish row would overwrite the property instead of joining it,
    and it would not read back as the Spanish text the operator approved.

    Only a plain string is stamped, and only when the row names a language: a
    value that already carries its own languages keeps them (they are the more
    specific statement), and a language-independent value (a select label, a
    date, a link) is left exactly as the script wrote it. A stray ``language`` on
    such a property is dropped by the write adapter's coercion, not by guessing
    here.
    """
    if language is None or not isinstance(value, str):
        return value
    return [{"value": value, "language": language}]


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


# Uwazi's refusal message is the whole response body (a JSON validation dump
# that runs to several hundred characters); the headline is in its first line.
_MAX_ERROR_CHARS = 400


def _failure_entry(entry: dict[str, Any]) -> dict[str, str]:
    """One refused write as ``{shared_id, error}``, with the error trimmed (pure)."""
    error = str(entry.get("error") or entry.get("error_code") or "write refused by Uwazi")
    return {
        "shared_id": str(entry.get("shared_id") or entry.get("id") or ""),
        "error": error if len(error) <= _MAX_ERROR_CHARS else error[:_MAX_ERROR_CHARS] + "…",
    }


def format_write_failures(failures: list[dict[str, str]], limit: int = 3) -> str:
    """One short line per refused write, capped, as the operator-facing message (pure)."""
    if not failures:
        return "no write failures"
    lines = [f"{failure['shared_id'] or '<unknown entity>'}: {failure['error']}" for failure in failures[:limit]]
    remaining = len(failures) - limit
    if remaining > 0:
        lines.append(f"(+{remaining} more)")
    return f"{len(failures)} write(s) refused by Uwazi — " + "; ".join(lines)


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


def pushable_rows(suggestions: list[Suggestion], selection: EntitySelection | None = None) -> list[Suggestion]:
    """The rows a Run would push, in order (pure).

    Only ``ACCEPTED``/``EDITED`` rows of executable ops (``update`` rows need a
    property name; ``create``/``delete``/``publish``/``unpublish``/
    ``set_publish_status``/``create_relationships`` rows are whole-entity).
    ``selection`` restricts to the checked (sharedId, language) rows when given;
    ``None`` means the whole set.
    """
    rows: list[Suggestion] = []
    for s in suggestions:
        if s.status not in _PUSHABLE:
            continue
        if not _selected(s, selection):
            continue
        if s.op == "update" and s.property_name is None:
            continue
        rows.append(s)
    return rows


def partition_for_apply(
    rows: list[Suggestion], selection: EntitySelection | None = None
) -> tuple[list[Suggestion], list[Suggestion]]:
    """(property_rows, whole_entity_rows) among pushable rows for the given subset (pure).

    ``property_rows`` = ACCEPTED/EDITED ``update`` rows with a ``property_name``
    — the partial-update path (``build_apply_updates`` groups them per entity).
    ``whole_entity_rows`` = everything else pushable: rows of the whole-entity
    ops (delete/publish/unpublish/set_publish_status/create_relationships),
    update rows without a property, and all ``create`` rows (a create's
    sharedId is the ``dry-created-N`` placeholder until Run mints a real one).
    """
    property_rows: list[Suggestion] = []
    whole_entity_rows: list[Suggestion] = []
    for suggestion in rows:
        if suggestion.status not in _PUSHABLE:
            continue
        if not _selected(suggestion, selection):
            continue
        if suggestion.op == "update" and suggestion.property_name is not None:
            property_rows.append(suggestion)
        else:
            whole_entity_rows.append(suggestion)
    return property_rows, whole_entity_rows


def build_apply_updates(suggestions: list[Suggestion], selection: EntitySelection | None = None) -> list[dict]:
    """Group pushable property suggestions into per-entity-LANGUAGE update payloads (pure).

    Rows group by (sharedId, language) into ONE ``{"shared_id", "language",
    "metadata": {...}}`` payload each (plus ``"title"`` when a ``_title`` row is
    present, and ``"template_name"`` when the recording carried one); an
    ``EDITED`` row overrides its proposal. Group and key order follow first
    appearance, so a batch is deterministic.

    Only ``update`` rows with a property name enter this grouping — the ops whose
    sharedId already identifies a real entity. ``create`` rows are property rows
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
    for suggestion in pushable_rows(suggestions, selection):
        if suggestion.op != _UPDATE_OP or suggestion.property_name is None:
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
            payload["metadata"][suggestion.property_name] = language_scoped_value(value, suggestion.language)
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


def build_apply_creates(suggestions: list[Suggestion], selection: EntitySelection | None = None) -> list[dict]:
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
    """
    grouped: dict[tuple[str, str | None], dict] = {}
    order: list[tuple[str, str | None]] = []
    for suggestion in pushable_rows(suggestions, selection):
        if suggestion.op != "create":
            continue
        key = (suggestion.shared_id, suggestion.language)
        if key not in grouped:
            grouped[key] = {"metadata": {}, "title": None, "template_name": None}
            order.append(key)
        payload = grouped[key]
        value = effective_value(suggestion)
        if suggestion.property_name == TITLE_PROPERTY:
            payload["title"] = value
        elif suggestion.property_name is not None:
            payload["metadata"][suggestion.property_name] = language_scoped_value(value, suggestion.language)
        if suggestion.template_name and not payload["template_name"]:
            payload["template_name"] = suggestion.template_name
    creates: list[dict] = []
    for key in order:
        shared_id, language = key
        payload = grouped[key]
        if not payload["template_name"]:
            raise ApplyRefusedError(f"create row {payload['title']!r} carries no template name; regenerate the task")
        entry: dict = {
            "title": payload["title"] or "",
            "template_name": payload["template_name"],
            "metadata": payload["metadata"],
            "language": language,
            "source_shared_id": shared_id,
        }
        creates.append(entry)
    return creates


class ApplySuggestionsUseCase:
    """Execute reviewed table rows through an intercepted run (revertable)."""

    def __init__(
        self,
        entity_api: EntityApiPort,
        relationship_api: RelationshipApiPort | None,
        entity_repository: EntityRepositoryPort,
        backup_store: BackupStorePort,
        audit_log: AuditLogPort | None,
        suggestion_store: SuggestionStorePort,
        file_repository=None,
        segmentation_repository=None,
        url_fetcher=None,
        cache_control: CacheInvalidationPort | None = None,
        cap: int = MAX_ENTITIES_PER_RUN,
        default_language: str = "en",
    ) -> None:
        self._entity_api = entity_api
        self._relationship_api = relationship_api
        self._entity_repository = entity_repository
        self._backup_store = backup_store
        self._audit_log = audit_log
        self._store: SuggestionStorePort = suggestion_store
        self._file_repository = file_repository
        self._segmentation_repository = segmentation_repository
        self._url_fetcher = url_fetcher
        self._cache_control = cache_control
        self._cap = cap
        self._default_language = default_language
        # Set by _build_crud: the event loop the sync CRUD helpers ride.
        self._crud_loop = None

    def apply(self, run_id: str, attempt: int, selection: EntitySelection = None) -> str:
        """Write the selected rows (optionally restricted to ``selection``).

        A Run accepts first (:meth:`_accept_pending`): the reviewed table
        commits through Run rather than per-row buttons, so the rows it pushes
        are still PENDING.

        The Run heuristic, all rows in one pass through ONE interceptor setup:
        property rows group into per-(entity, language) update payloads; create
        rows group into create payloads; delete/publish/unpublish/
        set_publish_status/create_relationships rows issue their dedicated bulk
        helper. Returns the apply run's id — an ordinary run that appears in the
        runs table and reverts through the existing revert use case.

        ``selection`` is the table's checked (sharedId, language) rows, so a
        translation task writes ONLY the languages the operator checked; None
        writes the whole attempt.

        Raises :class:`ApplyRefusedError` when there is nothing to run, and
        :class:`ApplyWriteError` when Uwazi refused a write — in which case
        only the writes that LANDED are marked ``APPLIED``; the rows of a refused
        entity keep their ``ACCEPTED`` status so the operator can run them again
        once the cause is fixed.
        """
        self._accept_pending(run_id, attempt, selection)
        rows = self._all_accepted(run_id, attempt)
        to_push = pushable_rows(rows, selection)
        _, whole_entity_rows = partition_for_apply(rows, selection)
        # Disjoint by construction: the update payloads hold real sharedIds
        # only, the creates the ``dry-created-N`` placeholders. A create
        # reaching ``updates`` would be enriched from a raw that does not
        # exist, so the two payloads are built from their own row sets.
        updates = self._enrich(build_apply_updates(rows, selection))
        creates = build_apply_creates(rows, selection) if any(r.op == "create" for r in whole_entity_rows) else []
        if not updates and not creates and not whole_entity_rows:
            raise ApplyRefusedError("nothing to run for the selected rows")

        manifest = self._prepare_apply_manifest(run_id, attempt, len(to_push))
        failures = self._write(run_id, manifest, updates, creates, whole_entity_rows)
        # A refused write changed nothing, so its rows must NOT read as applied:
        # they stay ACCEPTED and the operator can run them again. Every refusal
        # carries an identity the rows share (a real sharedId, or a create's
        # placeholder the result could not echo), so the split is by row, not by
        # guesswork.
        refused = {failure["shared_id"] for failure in failures if failure["shared_id"]}
        written = [row for row in to_push if row.shared_id not in refused]
        self._store.set_status([row.id for row in written if row.id is not None], SuggestionStatus.APPLIED)
        logger.info(
            "run={} pushed {} update payload(s), {} create(s), {} whole-entity row(s); {} write(s) refused",
            run_id,
            len(updates),
            len(creates),
            len(whole_entity_rows),
            len(failures),
        )
        if failures:
            for failure in failures:
                logger.error("run={} write refused shared_id={}: {}", run_id, failure["shared_id"], failure["error"])
            raise ApplyWriteError(failures)
        return run_id

    # --- internals -----------------------------------------------------------

    def _build_crud(self) -> tuple:
        """The sync CRUD helpers over one fresh loop + tool cache (pre-intercept).

        The loop is stored on ``self._crud_loop`` so :meth:`_write` can close it
        after the pass (the sync helpers' ``run_until_complete`` rides it).
        """
        self._crud_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._crud_loop)
        return _build_sync_crud_functions(
            self._entity_api, self._relationship_api, self._default_language, self._crud_loop, ToolCallCache()
        )

    def _build_intercept(self, run_id: str, manifest: MigrationManifest, loop) -> BackupIntercept:
        """Build the accumulating run's intercept, seeding the already-touched set.

        The touch set ALREADY in the manifest (an earlier Run against this same
        run) is passed as ``already_backed_up``: those entities keep their
        ORIGINAL first-touch snapshot, so a second write does not duplicate
        manifest entries and the revert restores the pre-Run state.
        """
        return BackupIntercept(
            entity_repository=self._entity_repository,
            backup_store=self._backup_store,
            manifest=manifest,
            run_id=run_id,
            language=self._default_language,
            loop=loop,
            audit_log=self._audit_log,
            cap=self._cap,
            file_repository=self._file_repository,
            cache_control=self._cache_control,
            already_backed_up=self._manifest_touched(manifest),
        )

    @staticmethod
    def _manifest_touched(manifest: MigrationManifest) -> set[str]:
        """Every sharedId the manifest already carries (pure)."""
        return (
            {e.shared_id for e in manifest.modified}
            | {e.shared_id for e in manifest.deleted}
            | {e.shared_id for e in manifest.created}
        )

    def _enrich(self, updates: list[dict]) -> list[dict]:
        """Fill in the fields the write port requires, in the payload's OWN language (pure-ish).

        Reviews only carry property values, but ``AgentEntity`` requires the
        entity's template, and a partial update must also carry the row locale.
        Reading the raw is the honest source for both (a recorded
        ``template_name`` wins when present) — and it is also what makes a
        suggestion pointing at a since-deleted entity fail with a clear error
        instead of silently writing nothing.

        The raw is read IN THE PAYLOAD'S LANGUAGE, because that is the row the
        write lands on: Uwazi keeps one entity-language row per translation and
        selects it through the write's ``locale`` cookie, so a Spanish value
        resolved against the default-language row would have its template taken
        from the wrong entity (and a missing row would report as "not found"
        only by accident). A payload whose language row does not exist fails
        here, loudly, rather than writing the text onto another language.

        Nothing is merged into the values: the reviewed value is the row's own
        single value, and Uwazi merges at the PROPERTY level server-side, so a
        property this Run does not touch keeps what the row already holds.
        """
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            enriched: list[dict] = []
            for update in updates:
                shared_id = update["shared_id"]
                language = str(update.get("language") or self._default_language)
                raw = loop.run_until_complete(self._entity_repository.get_raw_by_shared_id(shared_id, language))
                template_name = update.get("template_name") or raw.get("template") or raw.get("template_name")
                if not template_name:
                    raise ApplyRefusedError(f"cannot resolve a template for {shared_id!r}; nothing was applied to it")
                entry = {
                    "shared_id": shared_id,
                    "template_name": template_name,
                    "metadata": dict(update["metadata"]),
                    "language": raw.get("language") or language,
                }
                if "title" in update:
                    entry["title"] = update["title"]
                enriched.append(entry)
            return enriched
        finally:
            loop.close()

    def _accept_pending(self, run_id: str, attempt: int, selection: EntitySelection) -> None:
        """Accept the rows a Run is about to push — a Run IS the acceptance.

        The review UI commits through its Run action (the entity selection plus
        the confirm dialog), so the recorded rows it is about to push are still
        PENDING. Without this step every Run would be refused as "nothing
        accepted"; with it, the operator's selection and confirmation stand in
        for the row-level accept that the table no longer offers.

        Two source statuses are accepted: PENDING (never run) and REVERTED (a
        revert undid the write, so the row is runnable again — the operator can
        re-run a reverted row by row). DISCARDED rows stay refused, EDITED rows
        keep their edits, and APPLIED rows (still written) are never re-accepted.

        Scope follows the selection: the exact (sharedId, language) rows the operator
        checked, so running the ``es`` row of a translation task accepts ONLY
        that language's rows — its ``en`` sibling stays pending and is written
        by a second, separate Run. The whole attempt when None, nothing when
        empty.
        """
        if selection is not None and not selection:
            return
        for status in (SuggestionStatus.PENDING, SuggestionStatus.REVERTED):
            self._store.set_status_by_filter(
                run_id,
                attempt,
                SuggestionFilter(status=status, entity_languages=selection),
                SuggestionStatus.ACCEPTED,
            )

    def _all_accepted(self, run_id: str, attempt: int) -> list[Suggestion]:
        """Page through EVERY accepted/edited row (a Run covers the whole set)."""
        rows: list[Suggestion] = []
        page_size = 1000
        offset = 0
        while True:
            page, _total = self._store.query(
                run_id, attempt, SuggestionFilter(accepted_only=True, limit=page_size, offset=offset)
            )
            if not page:
                return rows
            rows.extend(page)
            offset += len(page)

    def _prepare_apply_manifest(self, run_id: str, attempt: int, entity_count: int) -> MigrationManifest:
        """Load the RUN'S OWN manifest and reuse it — a Run creates no run.

        A reviewed Run writes into the run it belongs to, so the tasks table
        gains no row: only the manifest's touch set, the snapshots, and the
        run's status change. The manifest found on disk is kept as-is (its
        script, prompt, and accumulated touch set intact — duplicates are
        prevented by the intercept's ``already_backed_up`` seed), so an earlier
        Run's writes stay revertable by the same revert. It is never cleared:
        its snapshots are the pre-first-touch truth this run reverts to.
        ``EXECUTED`` is the post-write state; a further Run simply writes into
        the same manifest again.
        """
        try:
            existing: MigrationManifest | None = self._backup_store.load_manifest(run_id)
        except FileNotFoundError:
            existing = None
        if existing is None:
            manifest = MigrationManifest(
                run_id=run_id,
                created_at=datetime.now(timezone.utc),
                prompt=f"Run reviewed rows of {run_id} (attempt {attempt}): {entity_count} row(s).",
                script="# reviewed-row execution: heuristic execution of the review table rows (no script)",
                status=RunStatus.PLANNED,
            )
        else:
            manifest = existing.model_copy(update={"status": RunStatus.PLANNED, "error": None, "error_step": None})
        self._backup_store.save_manifest(run_id, manifest)
        return manifest

    def _write(
        self,
        run_id: str,
        manifest: MigrationManifest,
        updates: list[dict],
        creates: list[dict],
        whole_entity_rows: list[Suggestion],
    ) -> list[dict[str, str]]:
        """Execute every row kind through ONE intercepted CRUD set, batched.

        Each call is snapshotted-then-written by the intercept, so an error
        mid-way leaves a partial but fully revertable run. Entities
        already snapshotted in THIS manifest (an earlier Run of the same run)
        are seeded as ``already_backed_up`` so no duplicate snapshot/manifest
        entry is made. Creates mint real sharedIds first; the manifest tracks
        them as ``created`` via the intercept.

        Updates and creates are issued ONE CALL PER LANGUAGE (:func:`language_groups`)
        because the write's ``locale`` cookie is what selects the entity-language
        row: a single call carrying both halves of a translation would write the
        Spanish text onto the English row.

        Returns the REFUSED writes (see :func:`write_failures`) instead of
        swallowing them. The helpers report a Uwazi rejection as ``success=False``
        rather than by raising, so a Run that ignored their return value reported
        a completed apply over an entity that was never changed. A refusal is not
        an exception here — the writes that DID land stay landed and revertable,
        so the run is still ``EXECUTED``, with the refusal recorded on the
        manifest for the run's error dialog.
        """
        crud = self._build_crud()
        loop = self._crud_loop
        intercept = self._build_intercept(run_id, manifest, loop)
        failures: list[dict[str, str]] = []
        try:
            decorated = intercept.decorate(crud)
            update_entities = decorated["update_entities"]
            create_entities = decorated["create_entities"]
            delete_entities = decorated["delete_entities"]
            set_publish_status = decorated["set_publish_status"]
            create_relationships = decorated["create_relationships"]

            for language, group in language_groups(updates, self._default_language):
                for offset in range(0, len(group), EXECUTE_BATCH_SIZE):
                    batch = group[offset : offset + EXECUTE_BATCH_SIZE]
                    failures.extend(write_failures(update_entities(batch, language)))
            for language, group in language_groups(creates, self._default_language):
                placeholders = [str(payload.get("source_shared_id") or "") for payload in group]
                for offset in range(0, len(group), EXECUTE_BATCH_SIZE):
                    batch = group[offset : offset + EXECUTE_BATCH_SIZE]
                    failures.extend(
                        write_failures(create_entities(batch, language), placeholders[offset : offset + len(batch)])
                    )

            delete_ids: list[str] = []
            publish_ids: list[str] = []
            unpublish_ids: list[str] = []
            relationships: list[dict] = []
            for row in whole_entity_rows:
                if row.op == "delete":
                    delete_ids.append(row.shared_id)
                elif row.op == "publish":
                    publish_ids.append(row.shared_id)
                elif row.op == "unpublish":
                    unpublish_ids.append(row.shared_id)
                elif row.op == "set_publish_status":
                    (publish_ids if row.proposed_value else unpublish_ids).append(row.shared_id)
                elif row.op == "create_relationships":
                    rel = row.proposed_value if isinstance(row.proposed_value, dict) else None
                    if rel is not None:
                        relationships.append(rel)
            if delete_ids:
                failures.extend(write_failures(delete_entities(delete_ids)))
            if publish_ids:
                failures.extend(write_failures(set_publish_status(publish_ids, True)))
            if unpublish_ids:
                failures.extend(write_failures(set_publish_status(unpublish_ids, False)))
            for offset in range(0, len(relationships), EXECUTE_BATCH_SIZE):
                batch = relationships[offset : offset + EXECUTE_BATCH_SIZE]
                sources = [str(rel.get("from_entity_shared_id") or "") for rel in batch]
                failures.extend(write_failures(create_relationships(batch), sources))
        except Exception as exc:
            intercept.manifest.status = RunStatus.FAILED
            intercept.manifest.error = f"{type(exc).__name__}: {exc}"
            intercept.manifest.error_step = "execute"
            self._backup_store.save_manifest(run_id, intercept.manifest)
            raise
        finally:
            loop.close()

        final = intercept.manifest
        final.status = RunStatus.EXECUTED
        final.last_executed_at = datetime.now(timezone.utc)
        # A partial refusal is still an executed run (the landed writes are
        # revertable), but the manifest must carry WHY it looks incomplete.
        final.error = format_write_failures(failures) if failures else None
        final.error_step = "execute" if failures else None
        self._backup_store.save_manifest(run_id, final)
        return failures


__all__ = [
    "ApplyRefusedError",
    "ApplySuggestionsUseCase",
    "ApplyWriteError",
    "EntitySelection",
    "build_apply_creates",
    "build_apply_updates",
    "effective_value",
    "format_write_failures",
    "language_groups",
    "language_scoped_value",
    "partition_for_apply",
    "pushable_rows",
    "stamp_language",
    "write_failures",
]
