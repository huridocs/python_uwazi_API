"""Execute reviewed table rows onto Uwazi as ONE revertable apply run.

The review UI separates *deciding* from *writing*: the table's rows are
precomputed by the planning pass (the recorded dry-run writes), and the
operator's Run action executes them with this module's DETERMINISTIC HEURISTIC
— no script runs, no LLM calls, no re-computation. Per row, the heuristic is:

- ``update`` rows with a property (or ``_title``): group per sharedId into one
  partial-update payload each (``build_apply_updates``, pure);
- ``create`` rows: group per placeholder sharedId into ``AgentEntityCreate``
  payloads and call ``create_entities`` through the intercept;
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


def merge_language_value(current: Any, proposed: Any) -> Any:
    """One property's value list with ``proposed``'s languages replacing current's (pure).

    Uwazi's partial update merges at the PROPERTY level
    (``merged_metadata.update(entity.metadata)``), so a payload carrying only the
    ``es`` entry of a text property would DROP the ``en`` entry that is already
    in Uwazi. Writing one language therefore means sending the whole list: the
    current entries of the OTHER languages, kept as they are, plus the proposed
    entries of the written ones.

    A language-independent value (a select, a link, a date — or a current value
    that is not a language list at all) is not merged: Uwazi replaces those
    whole, which is what the recorded proposal already means.
    """
    if not isinstance(proposed, list) or not value_languages(proposed):
        return proposed
    if not isinstance(current, list):
        return proposed
    written = set(value_languages(proposed))
    kept = [entry for entry in current if not (isinstance(entry, dict) and entry.get("language") in written)]
    return [*kept, *proposed]


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
    """Group pushable property suggestions into per-entity update payloads (pure).

    Rows group by sharedId into ONE ``{"shared_id", "metadata": {...}}`` payload
    (plus ``"title"`` when a ``_title`` row is present, and ``"template_name"``
    when the recording carried one); an ``EDITED`` row overrides its proposal.
    Group and key order follow first appearance, so a batch is deterministic.

    Only ``update`` rows with a property name enter this grouping — the ops whose
    sharedId already identifies a real entity. ``create`` rows are property rows
    too, but their sharedId is the ``dry-created-N`` placeholder, so they are
    grouped by :func:`build_apply_creates` (which needs no existing entity to
    read) and written through ``create_entities``.

    A value written in a language is stamped with it (:func:`stamp_language`)
    BEFORE the per-language merge, which is what lets the merge keep the
    entity's other languages and send one complete value list.

    Two checked rows for the same property in DIFFERENT languages (the operator
    took both halves of a translation) merge into ONE value list: a property
    holds one value per language, so ``es`` and ``en`` are two entries, not one
    row overwriting the other.
    """
    grouped: dict[str, dict] = {}
    order: list[str] = []
    for suggestion in pushable_rows(suggestions, selection):
        if suggestion.op != _UPDATE_OP or suggestion.property_name is None:
            continue
        if suggestion.shared_id not in grouped:
            grouped[suggestion.shared_id] = {"metadata": {}, "title": None, "template_name": None}
            order.append(suggestion.shared_id)
        payload = grouped[suggestion.shared_id]
        value = effective_value(suggestion)
        if suggestion.property_name == TITLE_PROPERTY:
            payload["title"] = value
        else:
            value = stamp_language(value, suggestion.language)
            existing = payload["metadata"].get(suggestion.property_name)
            payload["metadata"][suggestion.property_name] = (
                merge_language_value(existing, value) if existing is not None else value
            )
        if suggestion.template_name and not payload["template_name"]:
            payload["template_name"] = suggestion.template_name
    updates: list[dict] = []
    for shared_id in order:
        payload = grouped[shared_id]
        entry: dict = {"shared_id": shared_id, "metadata": payload["metadata"]}
        if payload["title"] is not None:
            entry["title"] = payload["title"]
        if payload["template_name"]:
            entry["template_name"] = payload["template_name"]
        updates.append(entry)
    return updates


def build_apply_creates(suggestions: list[Suggestion], selection: EntitySelection | None = None) -> list[dict]:
    """Group pushable create suggestions into per-entity create payloads (pure).

    A create's rows carry the same expansion an update does: one suggestion per
    metadata key + one ``_title`` row + the template per row. The FIRST row's
    ``template_name`` wins for the payload (a create is one template by
    definition); titles and metadata merge exactly like
    :func:`build_apply_updates` — including the language stamp
    (:func:`stamp_language`) and the per-language merge, which is how a create
    recorded with several languages in one property value writes them all in a
    single payload. Rows whose ``template_name`` is missing raise
    :class:`ApplyRefusedError` — a create cannot be written without a template.
    """
    grouped: dict[str, dict] = {}
    order: list[str] = []
    for suggestion in pushable_rows(suggestions, selection):
        if suggestion.op != "create":
            continue
        if suggestion.shared_id not in grouped:
            grouped[suggestion.shared_id] = {"metadata": {}, "title": None, "template_name": None}
            order.append(suggestion.shared_id)
        payload = grouped[suggestion.shared_id]
        value = effective_value(suggestion)
        if suggestion.property_name == TITLE_PROPERTY:
            payload["title"] = value
        elif suggestion.property_name is not None:
            value = stamp_language(value, suggestion.language)
            existing = payload["metadata"].get(suggestion.property_name)
            payload["metadata"][suggestion.property_name] = (
                merge_language_value(existing, value) if existing is not None else value
            )
        if suggestion.template_name and not payload["template_name"]:
            payload["template_name"] = suggestion.template_name
    creates: list[dict] = []
    for key in order:
        payload = grouped[key]
        if not payload["template_name"]:
            raise ApplyRefusedError(f"create row {payload['title']!r} carries no template name; regenerate the task")
        entry: dict = {
            "title": payload["title"] or "",
            "template_name": payload["template_name"],
            "metadata": payload["metadata"],
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
        property rows group into per-entity update payloads; create rows group
        into create payloads; delete/publish/unpublish/set_publish_status/
        create_relationships rows issue their dedicated bulk helper. Returns the
        apply run's id — an ordinary run that appears in the runs table and
        reverts through the existing revert use case.

        ``selection`` is the table's checked (sharedId, language) rows, so a
        translation task writes ONLY the languages the operator checked; None
        writes the whole attempt.
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
        self._write(run_id, manifest, updates, creates, whole_entity_rows)
        self._store.set_status([row.id for row in to_push if row.id is not None], SuggestionStatus.APPLIED)
        logger.info(
            "run={} pushed {} update payload(s), {} create(s), {} whole-entity row(s)",
            run_id,
            len(updates),
            len(creates),
            len(whole_entity_rows),
        )
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
        """Fill in the fields the write port requires, and keep other languages (pure-ish).

        Reviews only carry property values, but ``AgentEntity`` requires the
        entity's template, and a partial update must also carry the row locale.
        Reading the raw is the honest source for both (a recorded
        ``template_name`` wins when present) — and it is also what makes a
        suggestion pointing at a since-deleted entity fail with a clear error
        instead of silently writing nothing.

        The raw is ALSO what keeps a per-language write from destroying its
        siblings: Uwazi replaces a property's whole value list on update, so a
        payload holding only the ``es`` text would drop the ``en`` text already
        in the entity. Every per-language property is therefore merged with the
        entity's current value through :func:`merge_language_value` before it is
        sent, and only for the languages this Run actually wrote.
        """
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            enriched: list[dict] = []
            for update in updates:
                shared_id = update["shared_id"]
                raw = loop.run_until_complete(
                    self._entity_repository.get_raw_by_shared_id(shared_id, self._default_language)
                )
                template_name = update.get("template_name") or raw.get("template") or raw.get("template_name")
                if not template_name:
                    raise ApplyRefusedError(f"cannot resolve a template for {shared_id!r}; nothing was applied to it")
                raw_metadata = raw.get("metadata")
                current: dict[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}
                metadata = {key: merge_language_value(current.get(key), value) for key, value in update["metadata"].items()}
                entry = {
                    "shared_id": shared_id,
                    "template_name": template_name,
                    "metadata": metadata,
                    "language": raw.get("language") or self._default_language,
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
    ) -> None:
        """Execute every row kind through ONE intercepted CRUD set, batched.

        Each call is snapshotted-then-written by the intercept, so an error
        mid-way leaves a partial but fully revertable run. Entities
        already snapshotted in THIS manifest (an earlier Run of the same run)
        are seeded as ``already_backed_up`` so no duplicate snapshot/manifest
        entry is made. Creates mint real sharedIds first; the manifest tracks
        them as ``created`` via the intercept.
        """
        crud = self._build_crud()
        loop = self._crud_loop
        intercept = self._build_intercept(run_id, manifest, loop)
        try:
            decorated = intercept.decorate(crud)
            update_entities = decorated["update_entities"]
            create_entities = decorated["create_entities"]
            delete_entities = decorated["delete_entities"]
            set_publish_status = decorated["set_publish_status"]
            create_relationships = decorated["create_relationships"]

            for offset in range(0, len(updates), EXECUTE_BATCH_SIZE):
                update_entities(updates[offset : offset + EXECUTE_BATCH_SIZE])
            for offset in range(0, len(creates), EXECUTE_BATCH_SIZE):
                create_entities(creates[offset : offset + EXECUTE_BATCH_SIZE])

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
                delete_entities(delete_ids)
            if publish_ids:
                set_publish_status(publish_ids, True)
            if unpublish_ids:
                set_publish_status(unpublish_ids, False)
            for offset in range(0, len(relationships), EXECUTE_BATCH_SIZE):
                create_relationships(relationships[offset : offset + EXECUTE_BATCH_SIZE])
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
        final.error = None
        final.error_step = None
        self._backup_store.save_manifest(run_id, final)


__all__ = [
    "ApplyRefusedError",
    "ApplySuggestionsUseCase",
    "EntitySelection",
    "build_apply_creates",
    "build_apply_updates",
    "effective_value",
    "merge_language_value",
    "partition_for_apply",
    "pushable_rows",
    "stamp_language",
]
