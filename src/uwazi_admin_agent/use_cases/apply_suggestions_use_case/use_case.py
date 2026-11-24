"""The apply use case (payload helpers live in ``payloads``)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Callable

from loguru import logger

from uwazi_admin_agent.configuration import EXECUTE_BATCH_SIZE, MAX_ENTITIES_PER_RUN
from uwazi_admin_agent.domain.manifest import MigrationManifest, RunStatus
from uwazi_admin_agent.domain.select_values import (
    SELECT_TYPES,
)
from uwazi_admin_agent.domain.suggestion import (
    CREATE_OPS,
    PUBLISH_OPS,
    UPDATE_OPS,
    Suggestion,
    SuggestionFilter,
    SuggestionStatus,
)
from uwazi_admin_agent.ports.audit_log_port import AuditLogPort
from uwazi_admin_agent.ports.backup_store_port import BackupStorePort
from uwazi_admin_agent.ports.cache_invalidation_port import CacheInvalidationPort
from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort
from uwazi_admin_agent.ports.property_type_port import PropertyTypeLookupPort
from uwazi_admin_agent.ports.publish_state_port import PublishStatePort
from uwazi_admin_agent.ports.suggestion_store_port import SuggestionStorePort
from uwazi_admin_agent.ports.thesaurus_value_port import ThesaurusValueLookupPort
from uwazi_admin_agent.use_cases.backup_intercept import BackupIntercept
from uwazi_admin_agent.use_cases.file_transfer import move_files_for_target
from uwazi_admin_agent.use_cases.parallel_executor import ParallelExecutor
from uwazi_admin_agent.use_cases.script_exec_namespace import _build_sync_crud_functions
from uwazi_admin_agent.use_cases.throttle_controller import ThrottleController
from uwazi_agent.ports.entity_api_port import EntityApiPort
from uwazi_agent.ports.relationship_api_port import RelationshipApiPort
from uwazi_agent.use_cases.tools.tool_call_cache import ToolCallCache

from .errors import ApplyRefusedError, ApplyWriteError, format_write_failures
from .payloads import (
    _CREATE_OPS,
    EntitySelection,
    ProgressCallback,
    apply_units_total,
    build_apply_creates,
    build_apply_updates,
    file_move_targets,
    language_groups,
    merge_deletions,
    merge_moves,
    partition_for_apply,
    payload_units,
    property_scope_ids,
    pushable_rows,
    track_progress,
    write_failures,
)


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
        publish_state: PublishStatePort | None = None,
        property_types: PropertyTypeLookupPort | None = None,
        thesaurus_values: ThesaurusValueLookupPort | None = None,
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
        self._publish_state: PublishStatePort | None = publish_state
        # The template property-TYPE lookup: what lets a DATE-typed row's
        # recorded value be coerced to Uwazi's shape at the write boundary.
        # None → the write sends the value as recorded (the whole-entity path
        # still coerces through the mapper; the raw property-scoped path does
        # not, which is exactly what bit the extract-date task).
        self._property_types = property_types
        # The {label: uuid} thesaurus lookup for select/multiselect writes (None
        # = unwired; a select row then refuses to apply rather than writing
        # labels the instance would silently drop).
        self._thesaurus_values = thesaurus_values
        # Set by _build_crud: the event loop the sync CRUD helpers ride.
        self._crud_loop = None

    def apply(
        self,
        run_id: str,
        selection: EntitySelection = None,
        row_ids: list[int] | None = None,
        progress: ProgressCallback | None = None,
    ) -> str:
        """Write the selected rows (optionally restricted to ``selection``).

        A Run accepts first (:meth:`_accept_pending`): the reviewed table
        commits through Run rather than per-row buttons, so the rows it pushes
        are still PENDING.

        The Run heuristic, all rows in one pass through ONE interceptor setup:
        property rows group into per-(entity, language) update payloads; create
        rows group into create payloads; delete/publish/unpublish/
        set_publish_status/create_relationships rows issue their dedicated bulk
        helper. A merge row (`update_and_delete`/`create_and_delete`) writes its
        target, moves the sources' uploaded files onto it, and deletes the
        sources — all in this Run. Returns the apply run's id — an ordinary run
        that appears in the runs table and reverts through the existing revert
        use case.

        ``selection`` is the table's checked (sharedId, language) rows, so a
        translation task writes ONLY the languages the operator checked; None
        writes the whole table. ``row_ids`` narrows further to the checked
        SUBOPERATIONS (the review table's per-property rows), so running one
        property of one entity-language group writes exactly that property and
        leaves its siblings alone; None keeps the pair-level behaviour.

        ``progress`` is the optional ``(done, total)`` callback the review UI's
        progress toast reads while the writes go out: its unit is the
        SUBOPERATION (see :func:`apply_units_total`), reported after each batch
        lands so the fraction moves visibly, not once at the end.

        Raises :class:`ApplyRefusedError` when there is nothing to run, and
        :class:`ApplyWriteError` when Uwazi refused a write — in which case
        only the writes that LANDED are marked ``APPLIED``; the rows of a refused
        entity keep their ``ACCEPTED`` status so the operator can run them again
        once the cause is fixed.
        """
        self._accept_pending(run_id, selection, row_ids)
        rows = self._all_accepted(run_id)
        to_push = pushable_rows(rows, selection, row_ids)
        _, whole_entity_rows = partition_for_apply(rows, selection, row_ids)
        types = self._property_types_for_rows(rows)
        select_ids = self._select_value_ids(rows, types)

        # Disjoint by construction: the update payloads hold real sharedIds
        # only, the creates the ``dry-created-N`` placeholders. A create
        # reaching ``updates`` would be enriched from a raw that does not
        # exist, so the two payloads are built from their own row sets.
        updates = build_apply_updates(rows, selection, row_ids, property_types=types, select_value_ids=select_ids)
        # Merge rows write the target above and, in this SAME Run, delete their
        # sources — the part that makes a merge one action instead of a target
        # row plus N delete rows.
        merges = merge_deletions(to_push)
        # The entities nothing but a property write touches: they get the cheap
        # property-scoped snapshot and a direct raw write (no template read, no
        # client-side merge, no index wait). Computed BEFORE enrichment so the
        # scoped payloads skip the template read they do not need.
        scoped_ids = property_scope_ids(updates, whole_entity_rows, merges)
        updates = self._enrich(updates, skip=scoped_ids)
        creates = (
            build_apply_creates(rows, selection, row_ids, property_types=types)
            if any(r.op in _CREATE_OPS for r in whole_entity_rows)
            else []
        )
        # A standalone move_files row copies its sources' files onto the target
        # and leaves the source entities untouched.
        file_moves = file_move_targets(to_push)
        if (merges or file_moves) and self._file_repository is None:
            # Refuse BEFORE any write: a merge moves the sources' files onto the
            # target and only THEN deletes the sources, so without a file
            # repository the deletes would destroy files the operator approved
            # moving. A standalone file copy has the same hard dependency.
            # Failing loudly here is the same no-loss bias the script helper
            # keeps when it is called unwired.
            raise ApplyRefusedError(
                "merge/file-move rows need a wired file repository to copy the sources' files; "
                "wire FileRepositoryPort into the runtime to run them"
            )
        if not updates and not creates and not whole_entity_rows and not merges and not file_moves:
            raise ApplyRefusedError("nothing to run for the selected rows")

        manifest = self._prepare_apply_manifest(run_id, len(to_push))
        if selection is not None:
            selection_languages = {lang for _sid, lang in selection if lang}
        else:
            selection_languages = {row.language for row in to_push if row.language}
        # The progress denominator: SUBOPERATIONS, counted from the payloads
        # exactly the way _write counts them up as its batches land — so the
        # toast's fraction can never pass 1.0 by construction.
        units_total = apply_units_total(updates, creates, whole_entity_rows, merges, file_moves)
        # Starting the write pass ends the preflight phase: clear the gate
        # toast's note (the fraction takes over).
        self._report_progress(progress, 0, units_total, "")
        failures = self._write(
            run_id,
            manifest,
            updates,
            creates,
            whole_entity_rows,
            selection_languages,
            merges,
            file_moves=file_moves,
            progress=progress,
            units_total=units_total,
            property_scope=scoped_ids,
            capture_publish_state=any(row.op in PUBLISH_OPS for row in whole_entity_rows),
        )
        # The writes are over (landed or refused): fill the fraction so the
        # toast ends at its total whatever the per-batch accounting left it at,
        # and clear the phase note — the pass is done whatever it said.
        self._report_progress(progress, units_total, units_total, "")
        # A refused write changed nothing, so its rows must NOT read as applied:
        # they stay ACCEPTED and the operator can run them again. Every refusal
        # carries an identity the rows share (a real sharedId, or a create's
        # placeholder the result could not echo), so the split is by row, not by
        # guesswork.
        refused = {failure["shared_id"] for failure in failures if failure["shared_id"]}
        written = [row for row in to_push if row.shared_id not in refused]
        self._store.set_status([row.id for row in written if row.id is not None], SuggestionStatus.APPLIED)
        # ``current_value`` is NOT refreshed here on purpose. It is the row's
        # BEFORE state — the value the run is about to overwrite — and it is what
        # the review table's "Value" column and the entity window show, and what
        # the Revert puts back. Overwriting it with the value just written made
        # both surfaces display the post-run value ("before == proposed", match
        # "exact"), so the operator could no longer see what a revert restores.
        # A sync ("Sync with Uwazi") is the only thing that re-reads
        # live values, and the preflight still guards a Run against a stale row.
        logger.info(
            "run={} pushed {} update payload(s), {} create(s), {} whole-entity row(s), {} merge(s); {} write(s) refused",
            run_id,
            len(updates),
            len(creates),
            len(whole_entity_rows),
            len(merges),
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

    def _build_intercept(
        self,
        run_id: str,
        manifest: MigrationManifest,
        loop,
        affected_languages: set[str] | None = None,
        property_scope: set[str] | None = None,
        capture_publish_state: bool = True,
        note: Callable[[str], None] | None = None,
    ) -> BackupIntercept:
        """Build the accumulating run's intercept, seeding the already-touched set.

        The touch set ALREADY in the manifest (an earlier Run against this same
        run) is passed as ``already_backed_up``: those entities keep their
        ORIGINAL first-touch snapshot, so a second write does not duplicate
        manifest entries and the revert restores the pre-Run state. The
        snapshot covers EVERY configured language's row of each entity (the
        intercept fetches all of them), so a revert restores the entity in
        every language — not just the first-touched row's.

        ``property_scope`` is the set whose only writes are property updates;
        those take the cheap property-scoped snapshot instead (see
        :func:`property_scope_ids`). ``capture_publish_state`` is off unless the
        run contains a publish/unpublish row: the grants are a separate
        collection, so a pure property edit never needs the read.
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
            settings_languages=self._configured_languages(),
            affected_languages=affected_languages,
            publish_state=self._publish_state,
            property_scope_ids=property_scope,
            capture_publish_state=capture_publish_state,
            note=note,
        )

    def _configured_languages(self) -> list[str]:
        """The instance's configured language keys via the settings port (0 entries when unresolvable).

        Best-effort: the settings port may be unwired (older wirings) or
        unreadable; an empty list degrades the snapshot to the single-row
        behavior without failing the backup.
        """
        if self._entity_api is None:
            return []
        client = getattr(self._entity_api, "client", None)
        settings = getattr(client, "settings", None)
        if settings is None:
            return []
        try:
            languages = settings.get_languages()
        except Exception as exc:  # noqa: BLE001 — languages are best-effort
            logger.warning("could not read configured languages for snapshots: {}", exc)
            return []
        return [str(lang.key) for lang in languages if getattr(lang, "key", None)]

    @staticmethod
    def _manifest_touched(manifest: MigrationManifest) -> set[str]:
        """Every sharedId the manifest already carries (pure)."""
        return (
            {e.shared_id for e in manifest.modified}
            | {e.shared_id for e in manifest.deleted}
            | {e.shared_id for e in manifest.created}
        )

    def _enrich(self, updates: list[dict], skip: set[str] | None = None) -> list[dict]:
        """Fill in the fields the whole-entity write port requires (pure-ish).

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

        ``skip`` is the property-scoped set: those payloads are written straight
        through the raw repository, which needs no template, so reading their
        rows here would be a wasted round-trip (the intercept reads them for the
        snapshot anyway, and reads them FRESH). The reads that do happen run
        through the run's bounded pool instead of one at a time.

        Nothing is merged into the values: the reviewed value is the row's own
        single value, and Uwazi merges at the PROPERTY level server-side, so a
        property this Run does not touch keeps what the row already holds.
        """
        skip = skip or set()
        pending = [(index, update) for index, update in enumerate(updates) if update["shared_id"] not in skip]
        if not pending:
            return list(updates)
        raws = self._fetch_raws_parallel(
            [(update["shared_id"], str(update.get("language") or self._default_language)) for _, update in pending]
        )
        enriched = list(updates)
        for index, update in pending:
            shared_id = update["shared_id"]
            language = str(update.get("language") or self._default_language)
            raw = raws[(shared_id, language)]
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
            enriched[index] = entry
        return enriched

    def _fetch_raws_parallel(self, pairs: list[tuple[str, str]]) -> dict[tuple[str, str], dict[str, Any]]:
        """Read each ``(sharedId, language)`` raw once, fresh, through the bounded pool.

        The cache is dropped first for the ids being read: this read decides
        whether the write can happen at all, so it must see the live row rather
        than a TTL window (the same reason the snapshot path invalidates).
        """
        unique = list(dict.fromkeys(pairs))
        if self._cache_control is not None and unique:
            self._cache_control.invalidate_entities(sorted({shared_id for shared_id, _ in unique}))

        def _task(shared_id: str, language: str):
            def _run() -> dict[str, Any]:
                return asyncio.run(self._entity_repository.get_raw_by_shared_id(shared_id, language))

            return _run

        values, errors = ParallelExecutor(ThrottleController()).run([_task(sid, lang) for sid, lang in unique])
        raws: dict[tuple[str, str], dict[str, Any]] = {}
        for pair, value, error in zip(unique, values, errors):
            if error is not None:
                raise error
            raws[pair] = value
        return raws

    def _accept_pending(
        self,
        run_id: str,
        selection: EntitySelection,
        row_ids: list[int] | None = None,
    ) -> None:
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
        by a second, separate Run. The whole table when None, nothing when
        empty. ``row_ids`` narrows acceptance to the checked SUBOPERATIONS: the
        unchecked siblings of the same entity-language group stay pending, which
        is what makes "run this one property" mean exactly that.
        """
        if selection is not None and not selection:
            return
        if row_ids is not None and not row_ids:
            return
        for status in (SuggestionStatus.PENDING, SuggestionStatus.REVERTED):
            self._store.set_status_by_filter(
                run_id,
                SuggestionFilter(status=[status], entity_languages=selection, suggestion_ids=row_ids),
                SuggestionStatus.ACCEPTED,
            )

    def _all_accepted(self, run_id: str) -> list[Suggestion]:
        """Page through EVERY accepted/edited row (a Run covers the whole set)."""
        rows: list[Suggestion] = []
        page_size = 1000
        offset = 0
        while True:
            page, _total = self._store.query(run_id, SuggestionFilter(accepted_only=True, limit=page_size, offset=offset))
            if not page:
                return rows
            rows.extend(page)
            offset += len(page)

    def _prepare_apply_manifest(self, run_id: str, entity_count: int) -> MigrationManifest:
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
                prompt=f"Run reviewed rows of {run_id}: {entity_count} row(s).",
                script="# reviewed-row execution: heuristic execution of the review table rows (no script)",
                status=RunStatus.PLANNED,
            )
        else:
            if existing.status == RunStatus.REVERTED:
                # A fully reverted run starts a NEW execution: the old touch
                # set is history, not outstanding work, and its snapshots were
                # captured against a state that may have changed since. Clear
                # both so the table counts describe only this execution and a
                # fresh first-touch snapshot is taken.
                existing.reset_touch_set()
                self._backup_store.clear_run(run_id)
            manifest = existing.model_copy(update={"status": RunStatus.PLANNED, "error": None, "error_step": None})
        self._backup_store.save_manifest(run_id, manifest)
        return manifest

    def _move_merge_files(self, moves: list[tuple[str, list[str]]]) -> None:
        """Copy source files onto their target — a merge's move or a file copy.

        The apply-path twin of the script's ``move_files_to_entity``: it runs the
        SAME :func:`move_files_for_target` flow, so a UI merge/file copy
        preserves uploaded files exactly like the scripted one (re-upload each
        source document + uploaded attachment, SKIP byte-identical duplicates so
        merging duplicate entities never multiplies a shared file). ``moves`` is
        the pure ``(real_target, sources)`` list from :func:`merge_moves`: a
        merge's sources are deleted afterwards, a standalone copy's are not, but
        the copy itself is identical. Merge entries are already filtered to
        targets whose own write landed.

        Failure policy mirrors the script mover: a soft per-file failure is
        counted + logged but does NOT stop the run (a merge's sources are still
        deleted, the target keeps whatever moved), while a hard port exception
        propagates to :meth:`_write`'s handler — the run is marked FAILED and
        the source deletes never run, so a merge whose files could not move
        keeps its sources. A missing file repository is a no-op here;
        :meth:`apply` refuses the Run up front instead, so this guard only
        serves direct ``_write`` callers.
        """
        if not moves or self._file_repository is None:
            return
        run = self._crud_loop.run_until_complete
        totals = {"moved": 0, "skipped": 0, "failed": 0}
        for target, sources in moves:
            counts = move_files_for_target(
                self._entity_repository,
                self._file_repository,
                sources,
                target,
                self._default_language,
                run,
            )
            for key in totals:
                totals[key] += int(counts.get(key, 0))
            logger.info(
                "file move: target={} sources={} moved={} skipped={} failed={}",
                target,
                len(sources),
                counts.get("moved"),
                counts.get("skipped"),
                counts.get("failed"),
            )
        if totals["failed"]:
            logger.warning(
                "file move finished with {} un-copied file(s); for a merge, its sources are still deleted",
                totals["failed"],
            )

    def _report_progress(
        self,
        progress: ProgressCallback | None,
        done: int,
        total: int,
        note: str | None = None,
    ) -> None:
        """Emit one progress tick (best-effort: the write never fails over it)."""
        if progress is None:
            return
        try:
            if note is None:
                progress(done, total)
            else:
                progress(done, total, note)
        except Exception as exc:  # noqa: BLE001 — progress is display state, never an error state
            logger.warning("could not report apply progress ({} of {}): {}", done, total, exc)

    def _property_types_for_rows(self, rows: list[Suggestion]) -> dict[str, str]:
        """``{property_name: type}`` for the templates these rows touch (best-effort).

        One lookup per distinct ``template_name`` the recorded rows carry (the
        review table's Template column shows what the recording knew). The
        lookup rides the cached ``/api/templates`` read the label lookup
        already performed, so it costs no extra request. No lookup wired, an
        unresolvable template, or a lookup failure contributes nothing: those
        rows keep their recorded value and Uwazi's own validation gets the
        last word.
        """
        if self._property_types is None:
            return {}
        templates: list[str] = []
        for row in rows:
            if row.template_name and row.template_name not in templates:
                templates.append(row.template_name)
        types: dict[str, str] = {}
        for template in templates:
            try:
                types.update(self._property_types.property_types(template))
            except Exception as exc:  # noqa: BLE001 — a types read must never block a write pass
                logger.warning("apply: could not read property types for template {!r}: {}", template, exc)
        return types

    def _select_value_ids(self, rows: list[Suggestion], types: dict[str, str]) -> dict[tuple[str, str], dict[str, str]]:
        """``{(template, property): {label: uuid}}`` for the select/multiselect rows (best-effort).

        One lookup per distinct (template, property) pair; exceptions are
        logged and treated as an empty map, which makes the write refuse that
        property instead of guessing ids.
        """
        if self._thesaurus_values is None or not types:
            return {}
        pairs: list[tuple[str, str]] = []
        for row in rows:
            if row.op not in (UPDATE_OPS | CREATE_OPS) or not row.property_name:
                continue
            prop_type = types.get(row.property_name)
            if prop_type is None or prop_type not in SELECT_TYPES:
                continue
            key = (row.template_name or "", row.property_name)
            if key not in pairs:
                pairs.append(key)
        ids: dict[tuple[str, str], dict[str, str]] = {}
        for template, prop in pairs:
            try:
                ids[(template, prop)] = self._thesaurus_values.value_ids(template, prop)
            except Exception as exc:  # noqa: BLE001 — a thesaurus read must never block a write pass
                logger.warning("apply: could not read thesaurus values for {}/{}: {}", template, prop, exc)
                ids[(template, prop)] = {}
        return ids

    def _write(
        self,
        run_id: str,
        manifest: MigrationManifest,
        updates: list[dict],
        creates: list[dict],
        whole_entity_rows: list[Suggestion],
        affected_languages: set[str] | None = None,
        merges: list[tuple[str, list[str]]] | None = None,
        file_moves: list[tuple[str, list[str]]] | None = None,
        progress: ProgressCallback | None = None,
        units_total: int = 0,
        property_scope: set[str] | None = None,
        capture_publish_state: bool = True,
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

        ``merges`` is the ``(target, source ids)`` list from
        :func:`merge_deletions`: the target's files are moved onto it first
        (:meth:`_move_merge_files`), then each merge's sources are deleted in the
        SAME Run (one bulk call with every other delete). ``file_moves`` is the
        :func:`file_move_targets` list: the same copy, with no target write and
        no source delete. A merge target whose own write was refused does NOT
        have its sources moved or deleted — the merge never happened, so removing
        the sources would lose data the operator only approved moving. A
        standalone file copy has no target write to gate it.

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
        # The successful writes THIS pass appends are the entities that become
        # outstanding again (a re-run after a partial revert). Recorded here so
        # the manifest's reverted set can be pruned without guessing from the
        # per-batch refusal list (one entity can have both landed and refused
        # property writes).
        applied_before = len(manifest.applied_writes)
        # A progress callback gets a wrapper that remembers the latest tick, so
        # the interceptor's NOTE broadcasts (a bulk write refusing, say) carry
        # the live fraction instead of resetting it.
        units_so_far = [0]
        # Keep the CALLER's callback separate from the wrapper: :func:`track_progress`
        # captures it, so rebinding ``progress`` below never makes the wrapper
        # call itself (see that helper's docstring for the recursion it fixes).
        _tracked_progress = track_progress(progress, units_so_far)

        def _note(text: str) -> None:
            if progress is None:
                return
            try:
                progress(units_so_far[0], units_total, text)
            except Exception as exc:  # noqa: BLE001 — a note must never fail the write pass
                logger.warning("could not publish the gate note ({} of {}): {}", units_so_far[0], units_total, exc)

        _tracked_progress(0, units_total, "")
        # All subsequent tick sites go through the tracked wrapper.
        progress = _tracked_progress  # type: ignore[assignment]
        intercept = self._build_intercept(
            run_id,
            manifest,
            loop,
            affected_languages,
            property_scope=property_scope,
            capture_publish_state=capture_publish_state,
            note=_note,
        )
        # One bounded, 429-aware pool for the whole pass (the same controller the
        # script helpers use): property writes fan out up to its allowance
        # instead of going out strictly one at a time.
        executor = ParallelExecutor(ThrottleController())
        failures: list[dict[str, str]] = []
        failed_targets: set[str] = set()
        done_units = 0
        # placeholder (`dry-created-N`) -> the sharedId Uwazi minted for it, so a
        # create-merge can move files onto the entity that was actually created.
        created_ids: dict[str, str] = {}
        try:
            decorated = intercept.decorate(crud)
            create_entities = decorated["create_entities"]
            delete_entities = decorated["delete_entities"]
            set_publish_status = decorated["set_publish_status"]
            create_relationships = decorated["create_relationships"]

            for language, group in language_groups(updates, self._default_language):
                for offset in range(0, len(group), EXECUTE_BATCH_SIZE):
                    batch = group[offset : offset + EXECUTE_BATCH_SIZE]
                    # Property-scoped entities go straight through the raw
                    # repository (snapshot of the touched keys + one POST, no
                    # template read / client-side merge / index wait); the rest
                    # keep the whole-entity snapshot and the CRUD helper.
                    batch_failures = write_failures(intercept.write_property_updates(batch, language, executor))
                    failures.extend(batch_failures)
                    failed_targets.update(failure["shared_id"] for failure in batch_failures)
                    done_units += sum(payload_units(update) for update in batch)
                    self._report_progress(progress, done_units, units_total)
                    # Checkpoint after EVERY batch: a crash mid-Run then loses at
                    # most one batch's manifest entries instead of the whole
                    # touch set (the snapshot files are already on disk, so the
                    # manifest is what makes them revertable).
                    self._backup_store.save_manifest(run_id, intercept.manifest)
            for language, group in language_groups(creates, self._default_language):
                placeholders = [str(payload.get("source_shared_id") or "") for payload in group]
                for offset in range(0, len(group), EXECUTE_BATCH_SIZE):
                    batch = group[offset : offset + EXECUTE_BATCH_SIZE]
                    batch_placeholders = placeholders[offset : offset + len(batch)]
                    results = create_entities(batch, language)
                    batch_failures = write_failures(results, batch_placeholders)
                    failures.extend(batch_failures)
                    failed_targets.update(failure["shared_id"] for failure in batch_failures)
                    for placeholder, result in zip(batch_placeholders, results if isinstance(results, list) else []):
                        if isinstance(result, dict) and result.get("success") and result.get("shared_id"):
                            created_ids[placeholder] = str(result["shared_id"])
                    done_units += sum(payload_units(create) for create in batch)
                    self._report_progress(progress, done_units, units_total)

            # Copy each merge's source files onto its target BEFORE the sources
            # are deleted — the whole point of a merge: the sources' uploaded
            # files must survive their deletion. A standalone file-copy row runs
            # through the SAME mover (the sources keep their files). Runs after
            # the target writes (the merge script's order: update -> move ->
            # delete) and before any delete. A hard port failure raises here
            # (caught below), so the run is marked FAILED and NO source is
            # deleted, keeping its files.
            merged_moves = merge_moves(merges or [], failed_targets, created_ids)
            # A standalone file copy has no target write of its own, so a refused
            # write elsewhere must not suppress it.
            copied_moves = merge_moves(file_moves or [], None, created_ids)
            self._move_merge_files([*merged_moves, *copied_moves])
            if copied_moves and self._file_repository is not None:
                # A standalone file copy is ONE suboperation per target, exactly
                # as apply_units_total counted it. (A direct ``_write`` caller
                # without a file repository performs no copy, so it reports none.)
                done_units += len(copied_moves)
                self._report_progress(progress, done_units, units_total)

            delete_ids: list[str] = []
            publish_ids: list[str] = []
            unpublish_ids: list[str] = []
            relationships: list[dict] = []
            merge_units = 0
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
            row_delete_ids = list(delete_ids)
            for target, sources in merges or []:
                if target in failed_targets:
                    continue
                delete_ids.extend(sources)
                merge_units += len(sources)
            delete_ids = list(dict.fromkeys(delete_ids))
            if delete_ids:
                failures.extend(write_failures(delete_entities(delete_ids)))
                # The delete unit covers the ROW deletes here (each whole-entity
                # delete row is one suboperation) and the MERGE sources were
                # counted when their targets passed the failed-check above —
                # the same way apply_units_total counted them, so the fraction
                # can never pass 1.0.
                done_units += len(row_delete_ids) + merge_units
                self._report_progress(progress, done_units, units_total)
            if publish_ids:
                failures.extend(write_failures(set_publish_status(publish_ids, True)))
                done_units += len(publish_ids)
                self._report_progress(progress, done_units, units_total)
            if unpublish_ids:
                failures.extend(write_failures(set_publish_status(unpublish_ids, False)))
                done_units += len(unpublish_ids)
                self._report_progress(progress, done_units, units_total)
            for offset in range(0, len(relationships), EXECUTE_BATCH_SIZE):
                batch = relationships[offset : offset + EXECUTE_BATCH_SIZE]
                sources = [str(rel.get("from_entity_shared_id") or "") for rel in batch]
                failures.extend(write_failures(create_relationships(batch), sources))
                done_units += len(batch)
                self._report_progress(progress, done_units, units_total)
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
        # Every entity this pass wrote is outstanding again, so it leaves the
        # partial-revert set. Derive it from the successful writes (not the
        # refusal list): one entity may have landed one property and refused
        # another, and it is still modified.
        written_ids = {write.shared_id for write in final.applied_writes[applied_before:]}
        refused_ids = {failure["shared_id"] for failure in failures if failure["shared_id"]}
        written_ids.update(shared_id for shared_id in delete_ids if shared_id not in refused_ids)
        for target, sources in merges or []:
            if target in failed_targets:
                continue
            written_ids.add(target)
            written_ids.update(source for source in sources if source not in refused_ids)
        written_ids.update(target for target, _sources in (file_moves or []) if target not in failed_targets)
        written_ids.update(
            row.shared_id
            for row in whole_entity_rows
            if row.op == "create_relationships" and row.shared_id not in refused_ids
        )
        final.mark_written(sorted(written_ids))
        self._backup_store.save_manifest(run_id, final)
        return failures
