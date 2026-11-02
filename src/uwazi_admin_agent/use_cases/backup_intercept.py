"""The backup intercept — decorate CRUD helpers so real execution backs up by intercept (§2.4, Phase 4).

With a free-form script the touch set is emergent, so each mutating CRUD call
snapshots the raw before-state of the affected entity *before* applying. This
class wraps the reused sync CRUD helpers (from ``_build_sync_crud_functions``)
with that backup-before-apply semantics, populating
:class:`FilesystemBackupStore` + :class:`MigrationManifest` by construction.

The pure decision logic lives in :mod:`uwazi_admin_agent.domain.backup_decision`
(``decide_backup`` + ``populate_manifest`` + ``build_rewired_relationships``);
this class does the I/O: fetches raws via :class:`EntityRepositoryPort`, saves
snapshots via :class:`BackupStorePort`, and delegates to the underlying CRUD
helper. Not unit-tested (needs ports); validated via the simulation run.

This is also the write-path cache hook for sandbox CRUD writes: those go
through ``EntityApiPort`` (not the raw repository), so this intercept is the
only place that learns which entities a script mutated. Every intercepted
mutating op drops the affected entities' cached raws via
:class:`CacheInvalidationPort` right AFTER the underlying write (a failed
write changes nothing and invalidates nothing), keeping later reads honest.
File-row mutations ride the same hook: file rows are not entity rows (a
raw's ``documents``/``attachments`` are a runtime JOIN from the files
collection by sharedId), so a file DELETE is invisible to the entity-row
write paths — ``_record_deleted_files`` is the seam that invalidates the
affected entities' cached raws and evicts the deleted files' cached bytes
(the bytes are already in the backup store, so eviction is lossless).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Callable

from loguru import logger

from uwazi_admin_agent.configuration import BULK_UPDATE_MAX_IDS, ENTITY_CACHE_FRESH_SNAPSHOTS
from uwazi_admin_agent.domain.audit_record import AuditOutcome, AuditStep, make_audit_record
from uwazi_admin_agent.domain.backup_decision import (
    BackupDecision,
    build_rewired_relationships,
    decide_backup,
    populate_manifest,
)
from uwazi_admin_agent.domain.cap_enforcement import enforce_cap
from uwazi_admin_agent.domain.connection_hubs import extract_connection_hubs
from uwazi_admin_agent.domain.deleted_file import DeletedFile
from uwazi_admin_agent.domain.file_restore import extract_file_refs
from uwazi_admin_agent.domain.manifest import AppliedWrite, MigrationManifest
from uwazi_admin_agent.domain.property_restore import (
    bulk_property_groups,
    can_bulk_update,
    merge_touched_properties,
    minimal_property_row,
    row_snapshot_for_language,
)
from uwazi_admin_agent.domain.publish_state import PublishState
from uwazi_admin_agent.domain.snapshot import EntityRowSnapshot, EntitySnapshot, FileRef
from uwazi_admin_agent.ports.audit_log_port import AuditLogPort
from uwazi_admin_agent.ports.backup_store_port import BackupStorePort
from uwazi_admin_agent.ports.cache_invalidation_port import CacheInvalidationPort
from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort
from uwazi_admin_agent.ports.file_repository_port import FileRepositoryPort
from uwazi_admin_agent.ports.publish_state_port import PublishStatePort


class BackupIntercept:
    """Wrap CRUD helpers so every mutation snapshots before-state by intercept.

    Constructed per real-execution run with the live ports + the run's
    manifest. ``decorate(crud)`` returns the intercepted write-helper dict
    ready to bind into the exec namespace. Maintains ``_backed_up`` (the
    first-touch set) so only the first operation on each entity snapshots.
    """

    def __init__(
        self,
        entity_repository: EntityRepositoryPort,
        backup_store: BackupStorePort,
        manifest: MigrationManifest,
        run_id: str,
        language: str,
        loop: asyncio.AbstractEventLoop | None,
        audit_log: AuditLogPort | None = None,
        cap: int = 1000,
        file_repository: FileRepositoryPort | None = None,
        cache_control: CacheInvalidationPort | None = None,
        already_backed_up: set[str] | None = None,
        settings_languages: list[str] | None = None,
        affected_languages: set[str] | None = None,
        publish_state: PublishStatePort | None = None,
        property_scope_ids: set[str] | None = None,
        capture_publish_state: bool = True,
        note: Callable[[str], None] | None = None,
    ) -> None:
        self._entity_repository: EntityRepositoryPort = entity_repository
        self._backup_store: BackupStorePort = backup_store
        self._manifest: MigrationManifest = manifest
        self._run_id: str = run_id
        self._language: str = language
        self._loop: asyncio.AbstractEventLoop = loop
        # Entities already snapshotted in THIS manifest (an earlier accept of
        # the same accumulating apply run): they must NOT be re-snapshotted —
        # the first-touch snapshot is the pre-ANY-apply state the revert
        # restores, so a second snapshot would both duplicate the manifest
        # entry and capture a half-applied raw as "before".
        self._backed_up: set[str] = set(already_backed_up or ())
        # First-touch raws, kept in memory for the applied-write records: the
        # diff needs the PRE-RUN value of every property a payload touches, and
        # only the first-touch snapshot of an entity carries that (a second
        # write on the same entity must not read back its own output).
        self._raw_before: dict[str, dict[str, Any]] = {}
        self._audit_log: AuditLogPort | None = audit_log
        self._cap: int = cap
        self._file_repository: FileRepositoryPort | None = file_repository
        self._cache_control: CacheInvalidationPort | None = cache_control
        # Snapshot correctness beats speed: with the default on, _fetch_raws
        # drops the snapshotted entities' cached raws first so every backup
        # captures live server truth, not the TTL window (see configuration).
        self._snapshot_fresh: bool = ENTITY_CACHE_FRESH_SNAPSHOTS
        # Every configured language of the instance — the candidate set a
        # snapshot's per-language row fetch tries (requested first).
        self._settings_languages: list[str] = list(settings_languages or [])
        # The languages this run's rows were selected in (the task's affected
        # languages): recorded on the snapshot so revert summaries/tooltips can
        # name them.
        self._affected_languages: set[str] = set(affected_languages or ())
        # Publish/permission capture (None = unwired: snapshots carry no
        # publish_state and revert skips that restore, the previous behavior).
        self._publish_state: PublishStatePort | None = publish_state
        # First-touch snapshots taken in THIS session, by sharedId. Promotion
        # (an update-then-delete, or a refused-then-retried delete) needs the
        # snapshot to build the `deleted` entry without re-reading the row.
        self._snapshots: dict[str, EntitySnapshot] = {}
        # The entities whose ONLY writes in this pass are property updates (the
        # review table's bulk edit). They get a PROPERTY-SCOPED snapshot — just
        # the touched keys of the one row being written — instead of the whole
        # entity, which is what makes a 5000-entity run cheap in both requests
        # and disk. Empty for a script run: a script's touch set is emergent, so
        # its entities always need the full snapshot.
        self._property_scope_ids: set[str] = set(property_scope_ids or ())
        # Whether to read the entity's publish/permission grants into the
        # snapshot. `POST /api/entities` never writes them, so only a pass that
        # can publish/unpublish needs the extra read; a pure property edit
        # leaves it off (one request per entity saved).
        self._capture_publish: bool = capture_publish_state
        # The raw CRUD helpers, kept so a bulk property write can fall back to
        # the ordinary update path for the ids that need a full snapshot.
        self._crud: tuple | None = None
        # An optional operator-facing note sink (the review UI's live gate
        # toast): a bulk write refusing and falling back to per-entity writes
        # is reported HERE so the spinner explains itself while it happens.
        self._note: Callable[[str], None] | None = note

    def emit_note(self, text: str) -> None:
        """Publish one operator-facing progress note (best-effort, pure-ish)."""
        if self._note is None:
            return
        try:
            self._note(text)
        except Exception as exc:  # noqa: BLE001 — a note must never fail the write pass
            logger.debug("could not publish the gate note: {}", exc)

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Set the event loop used for raw fetches (called from the worker thread)."""
        self._loop = loop

    @property
    def manifest(self) -> MigrationManifest:
        """The live manifest, populated as the script runs."""
        return self._manifest

    def decorate(self, crud: tuple) -> dict[str, Any]:
        """Wrap the reused sync CRUD tuple with backup-before-apply."""
        self._crud = crud
        create_entities = crud[0]
        update_entities = crud[1]
        delete_entities = crud[2]
        publish_entities = crud[3]
        unpublish_entities = crud[4]
        set_publish_status = crud[5]
        create_relationships = crud[6]

        def create_entities_intercepted(entities_dicts: list[dict], language: str | None = None) -> list[dict]:
            results = create_entities(entities_dicts, language)
            created_ids = [sid for r in results if isinstance(r, dict) if r.get("success") if (sid := r.get("shared_id"))]
            self._record_created(results)
            self.record_applied_creates(entities_dicts, results, language)
            self._emit("create", created_ids)
            return results

        def update_entities_intercepted(entities_dicts: list[dict], language: str | None = None) -> list[dict]:
            ids = [sid for e in entities_dicts if (sid := e.get("shared_id"))]
            self._backup_before_modify(ids, language or self._language)
            result = update_entities(entities_dicts, language)
            self.record_applied_updates(entities_dicts, result, language)
            self._invalidate(ids)
            self._emit("update", ids)
            return result

        def delete_entities_intercepted(shared_ids: list[str], language: str | None = None) -> list[dict]:
            del language  # ignored: delete is by sharedId across ALL language rows
            self._backup_before_delete(shared_ids)
            result = delete_entities(shared_ids)
            self._record_deleted_entities(shared_ids, result)
            self._invalidate(shared_ids)
            self._emit("delete", shared_ids)
            return result

        def set_publish_status_intercepted(
            shared_ids: list[str], published: bool, language: str | None = None
        ) -> list[dict]:
            del language  # ignored: publish/unpublish act on all language rows by sharedId
            self._backup_before_modify(shared_ids, self._language)
            result = set_publish_status(shared_ids, published)
            self.record_applied_publish("publish" if published else "unpublish", shared_ids)
            self._invalidate(shared_ids)
            self._emit("set_publish_status", shared_ids)
            return result

        def publish_entities_intercepted(shared_ids: list[str], language: str | None = None) -> dict:
            del language
            self._backup_before_modify(shared_ids, self._language)
            result = publish_entities(shared_ids)
            self.record_applied_publish("publish", shared_ids)
            self._invalidate(shared_ids)
            self._emit("publish", shared_ids)
            return result

        def unpublish_entities_intercepted(shared_ids: list[str], language: str | None = None) -> dict:
            del language
            self._backup_before_modify(shared_ids, self._language)
            result = unpublish_entities(shared_ids)
            self.record_applied_publish("unpublish", shared_ids)
            self._invalidate(shared_ids)
            self._emit("unpublish", shared_ids)
            return result

        def create_relationships_intercepted(relationships_dicts: list[dict], language: str | None = None) -> list[dict]:
            from_ids = [sid for r in relationships_dicts if (sid := r.get("from_entity_shared_id"))]
            to_ids = [sid for r in relationships_dicts if (sid := r.get("to_entity_shared_id"))]
            self._backup_before_rewire(from_ids, language or self._language)
            result = create_relationships(relationships_dicts, language)
            # Relations denormalize onto BOTH endpoints' raws — invalidate both.
            self._invalidate(from_ids + to_ids)
            self._emit("create_relationships", from_ids)
            return result

        return {
            "create_entities": create_entities_intercepted,
            "update_entities": update_entities_intercepted,
            "delete_entities": delete_entities_intercepted,
            "publish_entities": publish_entities_intercepted,
            "unpublish_entities": unpublish_entities_intercepted,
            "set_publish_status": set_publish_status_intercepted,
            "create_relationships": create_relationships_intercepted,
        }

    def _backup_before_modify(self, shared_ids: list[str], language: str) -> dict[str, EntitySnapshot]:
        """Snapshot first-touch entities, add to manifest.modified; return them."""
        decision = decide_backup("update", shared_ids, self._created_set(), self._backed_up)
        return self._apply_snapshot_decision(decision, language)

    def _backup_before_delete(self, shared_ids: list[str]) -> dict[str, EntitySnapshot]:
        """Snapshot first-touch entities, add to manifest.deleted (or remove from created).

        For a delete, Uwazi tears down the entity's stored file bytes
        (``BulkCleanupEntityUseCase`` → ``deleteEntityFiles`` →
        ``deleteFilesFromStorage``), so this is the **only** path that captures
        file bytes into the backup (modified/rewire paths keep the entity and its
        files). For each first-touch deleted entity, after fetching the raw and
        building the snapshot, capture the uploaded files' bytes via
        :class:`FileRepositoryPort` and persist them via :class:`BackupStorePort`,
        attaching the :class:`FileRef` metadata list to the snapshot. Best-effort:
        a fetch failure drops that one file from the snapshot's ``files`` (revert
        will not try to re-upload bytes it does not have); the snapshot still saves.
        """
        decision = decide_backup("delete", shared_ids, self._created_set(), self._backed_up)
        self._escalate_property_scopes(decision.promote_to_deleted)
        return self._apply_snapshot_decision(decision, self._language, capture_files=True)

    def _escalate_property_scopes(self, shared_ids: list[str]) -> None:
        """Replace any property-scoped snapshot about to back a DELETE with a FULL one.

        A delete-revert RE-CREATES the entity from the snapshot raw, so promoting
        a property-scoped (minimal) raw would mint a broken entity: no relations,
        no untouched properties, no files. The review table never scopes an entity
        it also deletes (``property_scope_ids`` excludes it), but the invariant is
        enforced here too, where the damage would land — the full snapshot simply
        REPLACES the property one on disk and in memory, and the manifest entry is
        left alone so the delete decision promotes it as usual.
        """
        for shared_id in shared_ids:
            snapshot = self._snapshots.get(shared_id)
            if snapshot is None or snapshot.scope != "property":
                continue
            logger.info("backup: escalating {} from a property to a full snapshot (it is being deleted)", shared_id)
            raw = self._loop.run_until_complete(
                self._entity_repository.get_raw_by_shared_id(shared_id, snapshot.language or self._language)
            )
            self._save_snapshots([shared_id], {shared_id: raw}, capture_files=True)

    def _backup_before_rewire(self, from_ids: list[str], language: str) -> dict[str, EntitySnapshot]:
        """Snapshot first-touch from-entities, add to modified, record RewiredRelationship."""
        decision = decide_backup("create_relationships", from_ids, self._created_set(), self._backed_up)
        raws = self._fetch_raws(decision.snapshot_ids, language)
        snapshots = self._save_snapshots(decision.snapshot_ids, raws)
        self._raw_before.update({sid: raws[sid] for sid in snapshots})
        rewired = build_rewired_relationships(decision.snapshot_ids, raws, language)
        _ = populate_manifest(self._manifest, decision, snapshots)
        self._manifest.rewired.extend(rewired)
        self._enforce_cap()
        logger.debug("backup rewire: snapshotted {} from-entities, recorded {} rewired", len(snapshots), len(rewired))
        return snapshots

    def _apply_snapshot_decision(
        self, decision: BackupDecision, language: str, capture_files: bool = False
    ) -> dict[str, EntitySnapshot]:
        """Fetch raws, save snapshots, populate manifest for a backup decision.

        ``capture_files`` is set only on the delete path: after building each
        snapshot, fetch the uploaded files' bytes and attach the :class:`FileRef`
        metadata list so delete-revert can re-upload them.

        Returns the freshly-taken snapshots keyed by shared_id (first-touch only;
        an already-backed-up id is absent) and retains their raws in
        ``_raw_before`` so applied-write records can diff against pre-run values.
        """
        raws = self._fetch_raws(decision.snapshot_ids, language)
        snapshots = self._save_snapshots(decision.snapshot_ids, raws, capture_files=capture_files)
        self._raw_before.update({sid: raws[sid] for sid in snapshots})
        retained = {sid: self._snapshots[sid] for sid in decision.promote_to_deleted if sid in self._snapshots}
        _ = populate_manifest(self._manifest, decision, {**retained, **snapshots})
        self._enforce_cap()
        logger.debug("backup decision: snapshotted {} entities", len(snapshots))
        return snapshots

    # --- property-scoped snapshots + writes (the review table's bulk path) ---

    def write_property_updates(
        self,
        payloads: list[dict],
        language: str,
        executor: Any | None = None,
    ) -> list[dict]:
        """Write reviewed property payloads, backing up only what they touch.

        The review table's bulk edit path. A payload whose entity's ONLY writes
        in this pass are property updates takes a PROPERTY-SCOPED snapshot (the
        touched keys of the one row being written) and is written straight
        through ``save_raw_properties`` — no template read, no client-side
        metadata merge, no search-index wait. An entity also touched by a
        delete/publish/merge keeps the full-entity snapshot and the ordinary
        update helper, because its before-state must be the whole row.

        ``executor`` is the run's :class:`ParallelExecutor`; when given, the
        writes fan out up to the throttle's allowance (the same 429-aware
        bounded concurrency the script helpers use). The snapshots are taken
        BEFORE any write of the batch goes out, so a batch is never half-backed.

        Returns one result dict per payload (``shared_id``/``success``/``error``)
        — the same shape the reused CRUD helper returns, so the caller's refusal
        handling is unchanged.
        """
        scoped = [payload for payload in payloads if payload["shared_id"] in self._property_scope_ids]
        plain = [payload for payload in payloads if payload["shared_id"] not in self._property_scope_ids]
        results: list[dict] = []
        if scoped:
            results.extend(self._write_scoped_properties(scoped, language, executor))
        if plain:
            results.extend(self._write_full_updates(plain, language))
        return results

    def _write_scoped_properties(
        self,
        payloads: list[dict],
        language: str,
        executor: Any | None,
    ) -> list[dict]:
        """Snapshot the touched keys, then write one property payload per entity."""
        entries = [(payload["shared_id"], sorted(payload.get("metadata") or {})) for payload in payloads]
        self._backup_property_rows(entries, language, executor)
        plans = [self._property_write_plan(self._snapshots[payload["shared_id"]], payload, language) for payload in payloads]
        results = self._post_properties(plans, executor)
        self.record_applied_updates(payloads, results, language)
        ids = [payload["shared_id"] for payload in payloads]
        self._invalidate(ids)
        self._emit("update", ids)
        return results

    def _write_full_updates(self, payloads: list[dict], language: str) -> list[dict]:
        """The ordinary update path for entities that need a whole-entity snapshot."""
        update_entities = (self._crud or (None, None))[1]
        if update_entities is None:
            raise RuntimeError("backup intercept: write_property_updates needs decorate() first")
        ids = [payload["shared_id"] for payload in payloads]
        self._backup_before_modify(ids, language)
        result = update_entities(payloads, language)
        self.record_applied_updates(payloads, result, language)
        self._invalidate(ids)
        self._emit("update", ids)
        return result

    def _backup_property_rows(
        self,
        entries: list[tuple[str, list[str]]],
        language: str,
        executor: Any | None = None,
    ) -> dict[str, EntitySnapshot]:
        """Take/extend one property-scoped snapshot per (entity, row language).

        ``entries`` pairs each sharedId with the metadata keys this write touches.
        First touch captures only those keys' before-values; a LATER write on an
        entity that already has a property snapshot adds the keys it did not
        capture yet (their live values are still the pre-run ones — this run has
        not written them). An entity that already has a FULL snapshot needs
        nothing: it covers every key already.

        Falls back to the full-entity snapshot when the raw cannot support a
        property restore (no ``_id``, or no ``title`` — both required by Uwazi's
        update schema), so correctness never depends on the cheap shape.

        The reads go through the run's bounded pool when one is given: this is
        the ONLY read a property-scoped entity costs, so it is also the one that
        decides the pass's wall clock on a 5000-row table.
        """
        raws = self._fetch_rows_parallel([(shared_id, language) for shared_id, _ in entries], executor)
        snapshots: dict[str, EntitySnapshot] = {}
        for shared_id, touched in entries:
            raw = raws[(shared_id, language)]
            existing = self._existing_snapshot(shared_id)
            if existing is not None and existing.scope != "property":
                self._remember_before(shared_id, raw, touched)
                snapshots[shared_id] = existing
                continue
            row = minimal_property_row(raw, language, touched)
            if row is None:
                logger.warning(
                    "backup: {} cannot be property-scoped (no _id/title) — taking the full snapshot",
                    shared_id,
                )
                # ``add_modified`` is what makes the snapshot REVERTABLE: a
                # decision that only snapshots leaves the entity out of the
                # manifest, and revert never looks at it.
                snapshots.update(
                    self._apply_snapshot_decision(
                        BackupDecision(snapshot_ids=[shared_id], add_modified=[shared_id]),
                        language,
                    )
                )
                continue
            snapshot = self._merge_property_snapshot(existing, row, raw, touched, language)
            self._backup_store.save_snapshot(self._run_id, snapshot)
            self._snapshots[shared_id] = snapshot
            self._backed_up.add(shared_id)
            self._remember_before(shared_id, raw, touched)
            snapshots[shared_id] = snapshot
            if existing is None:
                _ = populate_manifest(self._manifest, BackupDecision(add_modified=[shared_id]), {shared_id: snapshot})
                self._enforce_cap()
        return snapshots

    def _fetch_rows_parallel(
        self,
        pairs: list[tuple[str, str]],
        executor: Any | None,
    ) -> dict[tuple[str, str], dict[str, Any]]:
        """Read each ``(sharedId, language)`` row once — concurrently when a pool is given.

        Snapshot reads bypass the raw cache when ``ENTITY_CACHE_FRESH_SNAPSHOTS``
        is on (the backup must capture live server truth), so they are real HTTP
        round-trips and the whole point of fanning them out. A failed read
        propagates: an entity that cannot be read cannot be backed up, and
        writing it without a snapshot is exactly what the backup exists to
        prevent.
        """
        unique = list(dict.fromkeys(pairs))
        if not unique:
            return {}
        if self._snapshot_fresh:
            self._invalidate(sorted({shared_id for shared_id, _ in unique}))
        if executor is None or len(unique) < 2:
            return {
                pair: self._loop.run_until_complete(self._entity_repository.get_raw_by_shared_id(pair[0], pair[1]))
                for pair in unique
            }

        def _task(shared_id: str, row_language: str):
            def _run() -> dict[str, Any]:
                return asyncio.run(self._entity_repository.get_raw_by_shared_id(shared_id, row_language))

            return _run

        values, errors = executor.run([_task(shared_id, row_language) for shared_id, row_language in unique])
        raws: dict[tuple[str, str], dict[str, Any]] = {}
        for pair, value, error in zip(unique, values, errors):
            if error is not None:
                raise error
            raws[pair] = value
        return raws

    def _merge_property_snapshot(
        self,
        existing: EntitySnapshot | None,
        row: EntityRowSnapshot,
        raw: dict[str, Any],
        touched: list[str],
        language: str,
    ) -> EntitySnapshot:
        """The snapshot to persist: the fresh property row, merged into any earlier one."""
        now = datetime.now(timezone.utc)
        if existing is None:
            return EntitySnapshot(
                shared_id=str(raw.get("sharedId")),
                internal_id=row.internal_id,
                language=row.language,
                raw=row.raw,
                captured_at=now,
                scope="property",
                rows=[row],
                affected_languages=sorted({row.language} & self._affected_languages),
            )
        rows: list[EntityRowSnapshot] = []
        for prior in existing.rows:
            if (prior.language or "") == (row.language or ""):
                rows.append(merge_touched_properties(prior, raw, touched))
            else:
                rows.append(prior)
        if not any((prior.language or "") == (row.language or "") for prior in existing.rows):
            rows.append(row)
        return existing.model_copy(update={"rows": rows, "captured_at": now})

    def _existing_snapshot(self, shared_id: str) -> EntitySnapshot | None:
        """The snapshot already on disk for ``shared_id`` (a prior Run into this manifest)."""
        if shared_id in self._snapshots:
            return self._snapshots[shared_id]
        try:
            snapshot = self._backup_store.load_snapshot(self._run_id, shared_id)
        except FileNotFoundError:
            return None
        self._snapshots[shared_id] = snapshot
        return snapshot

    def _remember_before(self, shared_id: str, raw: dict[str, Any], keys: list[str]) -> None:
        """Keep the before-values the applied-write diff needs — and nothing else.

        A property-scoped pass must NOT retain 5000 full raws in memory (that is
        the whole point of the cheap shape), so only the touched keys' values are
        kept. The first captured value wins: a second write on the same entity
        must diff against the pre-run state, not its own output.
        """
        kept = self._raw_before.setdefault(shared_id, {"title": raw.get("title"), "metadata": {}})
        source = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
        captured = kept.setdefault("metadata", {})
        for key in keys:
            if key not in captured and key in source:
                captured[key] = source[key]

    def _property_write_plan(
        self,
        snapshot: EntitySnapshot,
        payload: dict,
        language: str,
    ) -> dict[str, Any]:
        """One property write as a PLAN: the POST body plus what bulk grouping needs.

        Uwazi's update branch requires ``_id``, ``sharedId``, ``language`` and a
        non-empty ``title``; everything else is the reviewed metadata. The title
        is the payload's when the row sets one, otherwise the captured one — so
        a property-only write never blanks the entity's title.

        The plan also carries the ``template`` and ``language`` (the bulk
        endpoint groups ids by both) and ``title_changed``: a title write can
        never ride a bulk call, because ``multipleupdate`` has no way to set one.
        """
        row = row_snapshot_for_language(snapshot, language)
        internal_id = row.internal_id if row is not None else snapshot.internal_id
        row_raw = row.raw if row is not None else snapshot.raw
        captured_title = row_raw.get("title") or snapshot.raw.get("title")
        metadata = dict(payload.get("metadata") or {})
        title = payload.get("title") or captured_title or ""
        return {
            "shared_id": payload["shared_id"],
            "language": language,
            # The raw's template is Uwazi's own id; a recorded ``template_name``
            # is a display label and only a fallback, so two rows of one
            # template always group together.
            "template": row_raw.get("template") or payload.get("template_name"),
            "title_changed": bool(payload.get("title")),
            "metadata": metadata,
            "body": {
                "_id": internal_id,
                "sharedId": payload["shared_id"],
                "language": language,
                "title": title,
                "metadata": metadata,
            },
        }

    def _post_properties(self, plans: list[dict[str, Any]], executor: Any | None) -> list[dict]:
        """Write property plans: one bulk call per homogeneous group, else one POST each.

        The bulk endpoint is preferred even for a SINGLE id: it is one request
        either way, it never touches the entity's files, and it applies exactly
        the metadata the plan names. The per-entity POST remains for the plans
        the endpoint cannot express — a title write (``multipleupdate`` has no
        title input), a plan with no template, or an empty payload — and for the
        fallback of a refused bulk chunk. That POST path reads the live row to
        echo its files back, so it costs one extra read; keeping it off the
        common path is what makes a 5000-row pass ~2 requests per row instead of
        ~9.

        A bulk call that is refused (or that does not echo every id) falls back
        to the per-entity path for that chunk. The endpoint is all-or-nothing,
        and the retry writes the same values, so the fallback is idempotent and
        keeps per-entity error attribution.
        """
        results: list[dict] = []
        individual: list[dict[str, Any]] = []
        for group in bulk_property_groups(plans):
            if can_bulk_update(group[0]):
                results.extend(self._post_bulk(group, executor, individual))
            else:
                individual.extend(group)
        if individual:
            results.extend(self._post_individual(individual, executor))
        return results

    def _post_bulk(
        self,
        group: list[dict[str, Any]],
        executor: Any | None,
        fallback: list[dict[str, Any]],
    ) -> list[dict]:
        """One bulk call per chunk of ids; refused/incomplete chunks join ``fallback``."""
        results: list[dict] = []
        for start in range(0, len(group), BULK_UPDATE_MAX_IDS):
            chunk = group[start : start + BULK_UPDATE_MAX_IDS]
            language = str(chunk[0]["language"])
            try:
                updated = set(
                    self._loop.run_until_complete(
                        self._entity_repository.update_many_properties(
                            [plan["shared_id"] for plan in chunk],
                            dict(chunk[0]["metadata"]),
                            language,
                        )
                    )
                )
            except Exception as exc:  # noqa: BLE001 — a refused bulk call is a fallback, not a run failure
                logger.warning(
                    "backup: bulk property update refused for {} id(s) ({}); retrying per entity",
                    len(chunk),
                    exc,
                )
                self.emit_note("bulk write refused — applying per entity")
                fallback.extend(chunk)
                continue
            for plan in chunk:
                if plan["shared_id"] in updated:
                    results.append({"shared_id": plan["shared_id"], "success": True})
                else:
                    # Not echoed back: Uwazi did not report it updated. Retry it
                    # alone so the failure carries its own error text.
                    fallback.append(plan)
        if fallback:
            self.emit_note("bulk write refused — applying per entity")
        return results

    def _post_individual(self, plans: list[dict[str, Any]], executor: Any | None) -> list[dict]:
        """POST each property body, serially or through the run's bounded pool."""

        def _task(plan: dict[str, Any]):
            def _run() -> dict[str, Any]:
                asyncio.run(self._entity_repository.save_raw_properties(plan["body"]))
                return {"shared_id": plan["shared_id"], "success": True}

            return _run

        if executor is None or len(plans) < 2:
            results: list[dict] = []
            for plan in plans:
                try:
                    self._loop.run_until_complete(self._entity_repository.save_raw_properties(plan["body"]))
                    results.append({"shared_id": plan["shared_id"], "success": True})
                except Exception as exc:  # noqa: BLE001 — a refusal is a per-entity result, like the CRUD helper
                    results.append({"shared_id": plan["shared_id"], "success": False, "error": str(exc)})
            return results
        values, errors = executor.run([_task(plan) for plan in plans])
        results = []
        for plan, value, error in zip(plans, values, errors):
            if error is not None:
                results.append({"shared_id": plan["shared_id"], "success": False, "error": str(error)})
            elif isinstance(value, dict):
                results.append(value)
            else:
                results.append({"shared_id": plan["shared_id"], "success": True})
        return results

    def _record_created(self, results: list[dict]) -> None:
        """Record script-created shared_ids onto manifest.created (post-create)."""
        new_ids = [sid for r in results if isinstance(r, dict) if (sid := r.get("shared_id")) if r.get("success")]
        if not new_ids:
            return
        decision = BackupDecision(add_created=new_ids)
        _ = populate_manifest(self._manifest, decision, snapshots={})
        self._enforce_cap()
        logger.debug("backup intercept: recorded {} created entities", len(new_ids))

    def record_applied_publish(self, op: str, shared_ids: list[str]) -> None:
        """Record one publish-status write per id (all ids, success not derivable).

        The bulk publish endpoints return aggregate counts, not per-id outcomes,
        so a soft-failed id would be over-recorded. That is harmless and
        self-explaining: its diff renders before == after (nothing changed).
        """
        for sid in shared_ids:
            raw = self._raw_before.get(sid) or {}
            self._manifest.applied_writes.append(
                AppliedWrite(
                    op=op,  # type: ignore[arg-type]  # callers pass "publish"/"unpublish"
                    shared_id=sid,
                    language=self._language,
                    metadata_before={"published": raw.get("published")},
                )
            )

    def record_applied_updates(self, entities_dicts: list[dict], results: list[dict], language: str | None) -> None:
        """Record one applied write per SUCCESSFUL updated id, with before-values.

        ``entities_dicts`` is the exact payload the script sent (AgentEntity-shaped),
        ``results`` the per-id outcome dicts the underlying helper returned. The
        before-values come from the first-touch raw of that id — for an id the run
        created earlier there is none, so ``metadata_before`` is empty and every
        property reads as newly set, which is what happened.
        """
        by_id = {sid: e for e in entities_dicts if (sid := e.get("shared_id"))}
        for result in results:
            if not (isinstance(result, dict) and result.get("success")):
                continue
            sid = result.get("shared_id")
            entity = by_id.get(sid)
            if not sid or entity is None:
                continue
            self._manifest.applied_writes.append(
                self._build_applied_write(
                    op="update",
                    shared_id=sid,
                    language=entity.get("language") or language or self._language,
                    title=entity.get("title"),
                    template_name=entity.get("template_name"),
                    metadata=entity.get("metadata") or {},
                    before_raw=self._raw_before.get(sid),
                )
            )

    def record_applied_creates(self, entities_dicts: list[dict], results: list[dict], language: str | None) -> None:
        """Record one applied write per SUCCESSFULLY created id (no before-values).

        Creates have no pre-run state: Uwazi minted the sharedId, so there is no
        snapshot and no before-raw. Every property therefore reads as set from
        nothing, which is exactly the truth.
        """
        creates = list(entities_dicts)
        for index, result in enumerate(results):
            if not (isinstance(result, dict) and result.get("success")):
                continue
            sid = result.get("shared_id")
            if not sid:
                continue
            # Creates are applied one-per-dict in input order; a soft-failed create
            # does not shift the mapping because the adapter returns one result per
            # input dict, in that order.
            entity = creates[index] if index < len(creates) else {}
            self._manifest.applied_writes.append(
                self._build_applied_write(
                    op="create",
                    shared_id=sid,
                    language=entity.get("language") or language or self._language,
                    title=entity.get("title"),
                    template_name=entity.get("template_name"),
                    metadata=entity.get("metadata") or {},
                    before_raw=None,
                )
            )

    def _build_applied_write(
        self,
        op: str,
        shared_id: str,
        language: str,
        title: Any,
        template_name: Any,
        metadata: dict[str, Any],
        before_raw: dict[str, Any] | None,
    ) -> AppliedWrite:
        """Assemble one :class:`AppliedWrite`, restricting the before-state to the payload's keys.

        Only the properties the payload touched can be diffed, so only those keys
        are copied out of the before-raw — a big raw's untouched metadata never
        lands on the manifest.
        """
        before_metadata = (before_raw or {}).get("metadata") or {}
        return AppliedWrite(
            op=op,  # type: ignore[arg-type]  # callers pass "update"/"create"
            shared_id=shared_id,
            language=language,
            title=title if isinstance(title, str) else None,
            title_before=(before_raw or {}).get("title"),
            template_name=template_name if isinstance(template_name, str) else None,
            metadata=metadata,
            metadata_before={key: before_metadata.get(key) for key in metadata},
        )

    def _enforce_cap(self) -> None:
        """Raise :class:`CapExceededError` if the touch set exceeds the cap.

        Emits a ``cap_exceeded`` audit record (outcome=FAILURE) before raising
        so the audit log shows why the run halted. The raised error propagates
        as a script error and triggers the on-error policy.
        """
        try:
            enforce_cap(self._manifest, self._cap)
        except Exception as exc:
            self._emit("cap_exceeded", [], outcome=AuditOutcome.FAILURE, detail=str(exc))
            raise

    def _emit(
        self,
        op_kind: str,
        shared_ids: list[str],
        outcome: AuditOutcome = AuditOutcome.SUCCESS,
        detail: str | None = None,
    ) -> None:
        """Append one audit record for an intercepted op (no-op if no audit log)."""
        if self._audit_log is None:
            return
        self._audit_log.append(
            self._run_id,
            make_audit_record(
                run_id=self._run_id,
                step=AuditStep.EXECUTE,
                op_kind=op_kind,
                shared_ids=shared_ids,
                outcome=outcome,
                detail=detail,
            ),
        )

    def _fetch_raws(self, shared_ids: list[str], language: str) -> dict[str, dict[str, Any]]:
        """Fetch the full raw (with relations) for each id via the entity repository.

        Snapshot correctness beats speed: when ``ENTITY_CACHE_FRESH_SNAPSHOTS``
        is on (the default), each snapshotted entity's cached raws are dropped
        first so the backup comes from the live instance rather than the TTL
        window — the refetch then repopulates the cache, and the post-write
        invalidation clears that again, so the loop is self-correcting.
        """
        if self._snapshot_fresh:
            self._invalidate(shared_ids)
        raws: dict[str, dict[str, Any]] = {}
        for sid in shared_ids:
            raws[sid] = self._loop.run_until_complete(self._entity_repository.get_raw_by_shared_id(sid, language))
        return raws

    def _fetch_all_language_rows(
        self, shared_id: str, language: str, primary_raw: dict[str, Any] | None = None
    ) -> EntitySnapshot | None:
        """Fetch EVERY configured language's row for one sharedId (None = entity absent).

        The first-touch snapshot must cover the whole entity, not one row: a
        Run writes per (entity, language) ROW, so the same entity can be
        written once per language across rows — and a revert that restores a
        single row's raw leaves every OTHER language modified (the partial-
        revert bug). The order is the requested language first (it is also the
        ``raw``/``language`` fallback fields' row), then the instance's other
        configured languages. Rows that do not exist in one language are absent
        from ``rows`` (Uwazi keeps a row per language only when the entity was
        translated into it).

        ``primary_raw`` is the row the caller already fetched for the requested
        language. Reusing it saves one HTTP round-trip per snapshotted entity —
        the read is the same request the backup had to make anyway.
        """
        candidates = self._language_order(language)
        rows: list[EntityRowSnapshot] = []
        primary_language: str | None = None
        if primary_raw is not None:
            row_lang = primary_raw.get("language") if isinstance(primary_raw.get("language"), str) else language
            rows.append(
                EntityRowSnapshot(
                    language=row_lang or language,
                    internal_id=primary_raw.get("_id"),
                    raw=primary_raw,
                )
            )
            primary_language = row_lang or language
        for candidate in candidates:
            if primary_raw is not None and candidate == language:
                continue  # already captured above
            try:
                raw = self._loop.run_until_complete(self._entity_repository.get_raw_by_shared_id(shared_id, candidate))
            except Exception as exc:  # noqa: BLE001 — a missing row is normal, not a failure
                logger.debug("no row for sharedId={} language={}: {}", shared_id, candidate, exc)
                continue
            row_lang = raw.get("language") if isinstance(raw.get("language"), str) else candidate
            rows.append(EntityRowSnapshot(language=row_lang or candidate, internal_id=raw.get("_id"), raw=raw))
            if primary_language is None:
                primary_language = row_lang or candidate
        if not rows:
            return None
        primary_raw = rows[0].raw
        return EntitySnapshot(
            shared_id=shared_id,
            internal_id=primary_raw.get("_id"),
            language=primary_language,
            raw=primary_raw,
            captured_at=datetime.now(timezone.utc),
            rows=rows,
            affected_languages=sorted({r.language for r in rows} & set(self._affected_languages)),
        )

    def _language_order(self, requested: str) -> list[str]:
        """The configured languages to try for one entity, requested first.

        The instance's configured languages (via the settings repository) are
        the whole candidate set — every language the instance could hold a row
        for. Unreadable settings degrade to the requested language alone (the
        previous single-row behavior, still first-touch-safe).
        """
        languages: list[str] = []
        if self._settings_languages:
            languages = [*self._settings_languages]
        if requested in languages:
            languages.remove(requested)
        return [requested, *languages]

    def _save_snapshots(
        self, shared_ids: list[str], raws: dict[str, dict[str, Any]], capture_files: bool = False
    ) -> dict[str, EntitySnapshot]:
        """Build + persist EntitySnapshots; mark ids as backed up.

        When ``capture_files`` is set (delete path), also capture the uploaded
        files' bytes for each entity and attach the :class:`FileRef` metadata
        list to the snapshot (best-effort per file). The bytes live in the backup
        store as parallel binary artifacts; the snapshot JSON carries only the
        metadata, so raw fidelity (§2.5) is preserved and the snapshot stays
        human-readable.
        """
        snapshots: dict[str, EntitySnapshot] = {}
        now = datetime.now(timezone.utc)
        for sid in shared_ids:
            raw = raws[sid]
            # ONE first-touch snapshot per entity, carrying EVERY configured
            # language's row: the first-touch semantic stays (the second write
            # to another row of the same entity must not re-snapshot), but the
            # single snapshot now covers all rows so a revert restores the
            # entity whole. Falls back to the single fetched raw when the
            # all-language fetch failed (settings unreadable etc.) so the
            # backup never blocks the write.
            snapshot = self._fetch_all_language_rows(sid, raw.get("language") or self._language, primary_raw=raw)
            if snapshot is None:
                file_refs = self._capture_files(sid, raw) if capture_files else None
                snapshot = EntitySnapshot(
                    shared_id=sid,
                    internal_id=raw.get("_id"),
                    language=raw.get("language"),
                    raw=raw,
                    captured_at=now,
                    files=file_refs,
                )
            elif capture_files:
                snapshot = snapshot.model_copy(update={"files": self._capture_files(sid, snapshot.raw)})
            # Content is not the whole before-state: Uwazi's entity write ignores
            # `published`/`permissions`, and relationship rows that carry a file or
            # a text reference belong to no property. Capture both so revert can
            # actually undo a publish or a citation the run created.
            snapshot = snapshot.model_copy(
                update={
                    "publish_state": self._capture_publish_state(sid, snapshot.language) if self._capture_publish else None,
                    "connection_hubs": extract_connection_hubs(snapshot.raw.get("relations")),
                }
            )
            snapshot = snapshot.model_copy(update={"captured_at": now})
            self._backup_store.save_snapshot(self._run_id, snapshot)
            snapshots[sid] = snapshot
            self._snapshots[sid] = snapshot
            self._backed_up.add(sid)
        return snapshots

    def _capture_publish_state(self, shared_id: str, language: str | None) -> PublishState | None:
        """Read the entity's publish + permission grants (None when unwired/unreadable).

        Best-effort by design: this is an ADDITIONAL field on a snapshot whose
        content restore must never be blocked by it. An unwired port (tests, an
        older wiring) or a denied read degrades to ``None`` — revert then skips
        the publish restore, exactly as before this field existed.
        """
        if self._publish_state is None:
            return None
        try:
            return self._loop.run_until_complete(self._publish_state.read(shared_id, language))
        except Exception as exc:  # noqa: BLE001 — a backup must not be blocked by this read
            logger.warning("backup: publish state unavailable sharedId={}: {}", shared_id, exc)
            return None

    def _record_deleted_entities(self, shared_ids: list[str], results: list[dict]) -> None:
        """Drop REFUSED entities back out of ``manifest.deleted``.

        The snapshot decision runs BEFORE the delete (it must — the raw is only
        available until the row is gone), so it records every requested id as
        deleted. Uwazi can still refuse individual ids (permissions, a stale
        id): the helper reports them as ``success=False`` and the entity
        survives. Leaving them in ``manifest.deleted`` made revert RE-CREATE a
        live entity — a duplicate with a fresh sharedId. Only ids the helper
        explicitly reports as successful stay recorded; an id missing from the
        result is assumed applied (the bulk endpoint answers 'ok' for the whole
        batch and the per-id fallback reports every id).
        """
        refused = {
            str(r.get("shared_id"))
            for r in results
            if isinstance(r, dict) and r.get("shared_id") and r.get("success") is False
        }
        if not refused:
            return
        manifest_ids = {e.shared_id for e in self._manifest.deleted}
        removable = refused & manifest_ids
        if not removable:
            return
        self._manifest.deleted = [e for e in self._manifest.deleted if e.shared_id not in removable]
        self._emit("delete_refused", sorted(removable), outcome=AuditOutcome.FAILURE)
        logger.warning(
            "backup intercept: {} refused delete(s) removed from manifest.deleted: {}",
            len(removable),
            sorted(removable),
        )

    def _capture_files(self, shared_id: str, raw: dict[str, Any]) -> list[FileRef]:
        """Fetch + persist the uploaded files for a to-be-deleted entity.

        Returns the :class:`FileRef` metadata list to attach to the snapshot. If
        no file repository is wired (e.g. tests), returns an empty list so the
        snapshot records "no files captured" rather than ``None`` (``None`` means
        "not a delete-path snapshot"; an empty list means "delete-path, no files").
        Best-effort: a fetch failure for one file drops that file from the list
        and logs a warning; the snapshot still saves with the remaining refs.
        """
        if self._file_repository is None:
            return []
        refs = extract_file_refs(raw)
        captured: list[FileRef] = []
        for ref in refs:
            data = self._loop.run_until_complete(self._file_repository.get_file_bytes(ref.filename))
            if data is None:
                logger.warning("backup: file bytes not found sharedId={} filename={}", shared_id, ref.filename)
                continue
            self._backup_store.save_file_bytes(self._run_id, shared_id, ref.file_id, data)
            captured.append(ref)
        logger.debug("backup: captured {}/{} files sharedId={}", len(captured), len(refs), shared_id)
        return captured

    def _created_set(self) -> set[str]:
        """The set of shared_ids currently in manifest.created."""
        return {e.shared_id for e in self._manifest.created}

    def _save_file_backup(self, shared_id: str, file_id: str, data: bytes) -> None:
        """Persist one to-be-deleted file's bytes BEFORE the delete call.

        The deletion core's revert precondition: called on the WORKER thread
        with BYTES IN HAND (store writes are sync filesystem I/O — the worker
        cannot borrow the script's event loop), keyed
        ``(run_id, shared_id, file_id)`` exactly like the delete-path capture.
        The bytes of refused/soft-failed targets stay in the store as harmless
        orphans (``clear_run`` wipes them on re-execute). File rows are not
        entity writes, so NO entity snapshot is taken here — the entity raw
        is untouched by a file delete. The CACHE consequences (raw + byte
        eviction) are owned by :meth:`_record_deleted_files`, which runs only
        for the deletes that actually succeeded.
        """
        self._backup_store.save_file_bytes(self._run_id, shared_id, file_id, data)

    def _record_deleted_files(self, records: list[DeletedFile]) -> None:
        """Record successful file-deletes on the manifest + invalidate caches (script thread).

        Called by the bound file-deletion helpers AFTER the deletion batch
        joins — manifest writes never race the worker tasks (the same
        invariant the write helpers keep) — with one :class:`DeletedFile` per
        delete that SUCCEEDED (a soft-``False`` delete left the file in place;
        recording it would make revert re-upload a copy that never went away,
        a duplicate). Then the caches are dropped so the files-collection
        mutation is visible to the very next read: the affected entities'
        cached raws (all language rows — the JOIN view is shared across
        locales) and the deleted files' cached bytes (a stale raw's ghost ref
        plus its still-cached bytes would let a re-run re-attempt the finished
        delete). Cache eviction is lossless by construction — the cache is a
        read-through mirror, the truth is Uwazi + this run's backup store
        (the bytes were persisted BEFORE each delete), and entries repopulate
        lazily on the next read.

        Invalidation runs BEFORE the cap check (distinct entities — see
        :func:`touch_set_count`) and any hard-error re-raise (the helpers call
        this before ``_raise_first_write_error``), so a partial or
        cap-tripping batch leaves its APPLIED deletes recorded AND
        cache-fresh — revertable and re-runnable. The ``delete_file`` audit
        record still closes the method.
        """
        if not records:
            return
        self._manifest.deleted_files.extend(records)
        self._invalidate(sorted({r.shared_id for r in records}))
        self._invalidate_file_bytes([r.filename for r in records])
        self._invalidate_segmentations([r.file_id for r in records])
        self._enforce_cap()
        self._emit("delete_file", sorted({r.shared_id for r in records}))
        logger.debug(
            "backup intercept: recorded {} deleted file(s) on {} entity(ies)",
            len(records),
            len({r.shared_id for r in records}),
        )

    def _invalidate(self, shared_ids: list[str]) -> None:
        """Drop cached raws for ``shared_ids`` (no-op without a wired cache control).

        Script-created entities are never invalidated: a create mints a fresh
        sharedId nothing can be cached under yet.
        """
        if self._cache_control is None or not shared_ids:
            return
        self._cache_control.invalidate_entities(shared_ids)

    def _invalidate_file_bytes(self, filenames: list[str]) -> None:
        """Evict cached bytes of deleted files (no-op without a cache control).

        Only the delete seam calls this, and only for deletes that SUCCEEDED
        (their bytes are already in the run's backup store — eviction is
        lossless; a soft-failed delete left its file in place, so its cached
        bytes stay valid).
        """
        if self._cache_control is None or not filenames:
            return
        self._cache_control.invalidate_files(filenames)

    def _invalidate_segmentations(self, file_ids: list[str]) -> None:
        """Evict cached ready segmentations of deleted files (no-op without a cache control).

        Segmentation keys are the document ``file_id``, so the delete seam —
        which already carries every deleted file's ``file_id`` — evicts its
        segmentation here, mirroring :meth:`_invalidate_file_bytes`. Only
        SUCCEEDED deletes are recorded (a soft-failed delete left its file in
        place, so its segmentation stays valid).
        """
        if self._cache_control is None or not file_ids:
            return
        self._cache_control.invalidate_segmentations(file_ids)
