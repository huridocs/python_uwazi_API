"""The dummy-entity validation harness (§2.7, Phase 3).

Creates throwaway dummy entities in the **real instance**, runs the candidate
script against them inside the dummy-scoped exec namespace, reverts each
original dummy to its exact before raw, checks that revert restored the exact
original state, and **always** deletes every dummy (originals + any the script
created) — on success and on failure. The pure outcome assembly
(:func:`build_validation_outcome`) is unit-tested separately; this module
gathers the raw dicts from Uwazi and is validated via the simulation run.

Revert here is the *proof* that the script's changes are reversible: restore
each original dummy by posting its before raw via ``EntityRepositoryPort.save_raw``
(the full raw carries ``relations``, so rewired relationships restore too —
raw round-trip is lossless per §8). This is the simpler, manifest-free counterpart
of Phase 4's intercept+manifest revert builder, which is the production path.

ES consistency (Option A): a revert re-indexes the dummy in ES (a newer
unrefreshed version); the immediately-following ``deleteByQuery`` (``conflicts:
'proceed'``) then skips it on version conflict, leaving an orphan. The harness
prevents this by (A) skipping the no-op revert for unchanged dummies and
(B) settling ES to the latest ``editDate`` before the cleanup delete. See
:mod:`uwazi_admin_agent.domain.search_probe` for the full rationale.

Not unit-tested (it needs the real instance); the DoD covers the pure parts only.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from loguru import logger

from uwazi_admin_agent.configuration import ES_SETTLE_POLL_INTERVAL_MS, ES_SETTLE_TIMEOUT_MS
from uwazi_admin_agent.domain.search_probe import (
    FreshnessResult,
    build_freshness_result,
    entity_unchanged,
    format_freshness_warning,
    max_edit_date_per_shared_id,
)
from uwazi_admin_agent.domain.validation_result import build_validation_outcome
from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort
from uwazi_admin_agent.ports.search_probe_port import SearchProbePort
from uwazi_admin_agent.use_cases.script_exec_namespace import build_exec_namespace, run_script_sync
from uwazi_agent.domain.agent_entity import AgentEntity
from uwazi_agent.domain.agent_entity_create import AgentEntityCreate
from uwazi_agent.domain.agent_entity_mutation_result import AgentEntityMutationResult
from uwazi_agent.ports.entity_api_port import EntityApiPort
from uwazi_agent.ports.relationship_api_port import RelationshipApiPort
from uwazi_agent.use_cases.tools.tool_call_cache import ToolCallCache

# Sentinel title prefix so dummies are identifiable in the instance and clearly
# throwaway. Uwazi assigns the sharedId on creation; we record it from the result.
_DUMMY_TITLE_PREFIX = "[uwazi-admin-agent-dummy] "


def resolve_recreated_fetch_ids(old_ids: list[str], recreated: dict[str, str]) -> list[tuple[str, str]]:
    """Map each original id to the id to fetch for the post-revert snapshot.

    For an original the script DELETED, revert re-creates it via the create
    branch with a **fresh** sharedId (recorded in ``recreated`` as ``old -> new``).
    The post-revert snapshot must then fetch the re-created entity by its **new**
    id (the old row is gone) and key the result under the **old** id, so the
    restore-equality comparison sees the re-created raw (DATA-only, identity
    excluded - see :mod:`domain.validation_result`) instead of ``None``.

    Pure: the testable seam extracted from :class:`DummyEntityHarness`'s revert
    step (the harness itself needs the real instance and is not unit-tested).
    """
    return [(sid, recreated.get(sid, sid)) for sid in old_ids]


class DummyEntityHarness:
    """Create dummies, run the candidate script, revert, exact-restore-check, cleanup.

    Constructed per validation call with the live ports the script's bound helpers
    will use (``entity_api``/``relationship_api``) plus the admin agent's raw
    repository (``entity_repository``) for snapshot/revert. ``language`` is the
    row locale for create/read/revert.
    """

    def __init__(
        self,
        entity_api: EntityApiPort,
        relationship_api: RelationshipApiPort | None,
        entity_repository: EntityRepositoryPort,
        language: str = "en",
        search_probe: SearchProbePort | None = None,
    ) -> None:
        self._entity_api: EntityApiPort = entity_api
        self._relationship_api: RelationshipApiPort | None = relationship_api
        self._entity_repository: EntityRepositoryPort = entity_repository
        self._language: str = language
        # Optional ES search probe for the freshness settle (Option A). ``None`` ->
        # the settle is a no-op (backward-compatible; the gate keeps working without
        # the orphan-race fix, e.g. in unit-test wiring that has no live ES).
        self._search_probe: SearchProbePort | None = search_probe

    async def run(self, script: str, dummy_spec: list[AgentEntityCreate]) -> Any:
        """Validate ``script`` against dummies built from ``dummy_spec``.

        Returns a :class:`ValidationResult`. Dummies are always deleted
        (originals + script-created), even on script error or cleanup error;
        a cleanup error is recorded on the result but does not mask the gate
        outcome.
        """
        created_ids: list[str] = []
        scope: set[str] = set()
        before: dict[str, dict[str, Any]] = {}
        after: dict[str, dict[str, Any] | None] = {}
        post_revert: dict[str, dict[str, Any] | None] = {}
        script_result: str | None = None
        script_error: str | None = None
        script_created_ids: list[str] = []
        cleanup_error: str | None = None
        es_settle_warning: str | None = None

        try:
            created_ids = await self._create_dummies(dummy_spec)
            scope.update(created_ids)
            # Settle after create so the script's own deletes (and the final
            # cleanup delete) see the just-indexed ES docs. Visibility suffices
            # here (target 0 = "editDate present"): the create is the first
            # version, so there is no newer unrefreshed version to conflict with
            # a delete (Option A, part B).
            create_settle = await self._wait_for_es_fresh({sid: 0 for sid in created_ids})
            if create_settle is not None and create_settle.timed_out and create_settle.pending_ids:
                es_settle_warning = format_freshness_warning("create", create_settle)
            before = await self._snapshot_raws(created_ids)
            dummy_entities = await self._fetch_dummy_entities(created_ids)

            script_result, script_error, script_created_ids = await self._run_script(script, scope, dummy_entities)
            after = await self._snapshot_raws_optional(scope)

            if script_error is None:
                recreated = await self._revert_originals(before, after, scope)
                post_revert = await self._snapshot_raws_optional_mapped(list(before.keys()), recreated)
        except Exception as exc:  # noqa: BLE001 — harness-level error must not escape without cleanup
            script_error = script_error or f"Harness error: {type(exc).__name__}: {exc}"
            logger.error("dummy harness error: {}", exc)
        finally:
            try:
                # Settle-then-delete (Option A, part B): wait for ES to reflect the
                # LATEST write to each alive dummy (the revert's re-index is the
                # typical latest) before the sharedId-scoped deleteByQuery runs.
                # deleteByQuery snapshots the refreshed index and skips docs whose
                # version advanced since the snapshot (conflicts: 'proceed');
                # settling to the latest editDate makes the snapshot see that latest
                # version so it is removed cleanly (no orphan). Targets come only
                # from alive raws (after/post_revert non-None values), so dead ids
                # (script-deleted, not re-created) are not polled to a timeout.
                # Runs in finally so the cleanup is race-free even on error paths.
                alive_raws = [r for r in (*after.values(), *post_revert.values()) if r]
                targets = max_edit_date_per_shared_id(alive_raws)
                cleanup_settle = await self._wait_for_es_fresh(targets)
                if (
                    cleanup_settle is not None
                    and cleanup_settle.timed_out
                    and cleanup_settle.pending_ids
                    and es_settle_warning is None
                ):
                    es_settle_warning = format_freshness_warning("cleanup", cleanup_settle)
                await self._delete_all(list(scope))
            except Exception as exc:  # noqa: BLE001
                cleanup_error = f"Cleanup failed: {type(exc).__name__}: {exc}"
                logger.error("dummy cleanup failed: {}", exc)

        if script_error is not None:
            post_revert = dict(before)  # revert not attempted; don't report a restore mismatch
        result = build_validation_outcome(
            script_result=script_result,
            script_error=script_error,
            before=before,
            after=after,
            post_revert=post_revert,
            created_shared_ids=script_created_ids,
            es_settle_warning=es_settle_warning,
        )
        result = result.model_copy(update={"cleanup_error": cleanup_error})
        return result

    async def _create_dummies(self, dummy_spec: list[AgentEntityCreate]) -> list[str]:
        """Create the dummy entities and return their assigned shared_ids."""
        specs = [self._tag_as_dummy(s) for s in dummy_spec]
        results: list[AgentEntityMutationResult] = await self._entity_api.create_entities(specs, self._language)
        ids: list[str] = []
        for r in results:
            if r.success and r.shared_id:
                ids.append(r.shared_id)
            else:
                logger.warning("dummy creation did not succeed: {}", r)
        if not ids:
            raise RuntimeError("No dummy entities were created (create_entities returned no successful shared_ids).")
        logger.info("created {} dummy entities", len(ids))
        return ids

    @staticmethod
    def _tag_as_dummy(spec: AgentEntityCreate) -> AgentEntityCreate:
        """Prefix the dummy title so it is clearly throwaway in the instance."""
        title = spec.title if spec.title.startswith(_DUMMY_TITLE_PREFIX) else _DUMMY_TITLE_PREFIX + spec.title
        return spec.model_copy(update={"title": title})

    async def _snapshot_raws(self, shared_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Fetch the full raw (with relations) for each id — the exact before-state."""
        out: dict[str, dict[str, Any]] = {}
        for sid in shared_ids:
            out[sid] = await self._entity_repository.get_raw_by_shared_id(sid, self._language)
        return out

    async def _snapshot_raws_optional(self, shared_ids: list[str]) -> dict[str, dict[str, Any] | None]:
        """Like :meth:`_snapshot_raws` but a missing (deleted) entity maps to ``None``."""
        out: dict[str, dict[str, Any] | None] = {}
        for sid in shared_ids:
            try:
                out[sid] = await self._entity_repository.get_raw_by_shared_id(sid, self._language)
            except Exception:  # noqa: BLE001 — deleted-by-script is expected, map to None
                out[sid] = None
        return out

    async def _fetch_dummy_entities(self, shared_ids: list[str]) -> list[AgentEntity]:
        """Fetch the dummies as :class:`AgentEntity` for the scoped ``query_entities``."""
        return await self._entity_api.get_entities_by_shared_ids(shared_ids=shared_ids, language=self._language)

    async def _run_script(
        self,
        script: str,
        scope: set[str],
        dummy_entities: list[AgentEntity],
    ) -> tuple[str | None, str | None, list[str]]:
        """``exec`` the script in a worker thread with a dedicated event loop.

        Returns ``(result, error, created_ids)`` where ``created_ids`` are the
        shared_ids the scoped ``create_entities`` added to ``scope`` during the run.
        """
        known_before = set(scope)

        def _exec() -> tuple[str | None, str | None]:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                namespace = build_exec_namespace(
                    entity_api=self._entity_api,
                    relationship_api=self._relationship_api,
                    loop=loop,
                    scope=scope,
                    dummy_entities=dummy_entities,
                    tool_cache=ToolCallCache(),
                    default_language=self._language,
                )
                return run_script_sync(script, namespace)
            finally:
                loop.close()

        result, error = await asyncio.to_thread(_exec)
        created_ids = [sid for sid in scope if sid not in known_before]
        if error:
            logger.warning("validation script raised: {}", error.splitlines()[0] if error else error)
        return result, error, created_ids

    async def _revert_originals(
        self,
        before: dict[str, dict[str, Any]],
        after: dict[str, dict[str, Any] | None],
        scope: set[str],
    ) -> dict[str, str]:
        """Restore each original dummy to its exact before raw (full raw, incl. relations).

        An original the script **deleted** (``after`` is None) cannot be restored
        via the update branch (its row is gone, so save_raw 422s); it is
        re-created via the create branch, minting a new sharedId. The new id is
        returned (``old -> new``) so the caller can fetch the re-created entity by
        its new id for the post-revert snapshot, and added to ``scope`` so the
        §2.7 cleanup (``_delete_all(list(scope))``) still deletes the re-created
        dummy - otherwise it would escape cleanup.

        An original the script did **not** modify (``before == after`` excl.
        platform-managed) is left untouched: its Mongo row is already at the
        before-state, so ``save_raw`` would be a no-op that only bumps ``editDate``
        and re-indexes ES - a newer unrefreshed version that the cleanup
        ``deleteByQuery`` would skip on version conflict (the orphan root cause).
        Skipping it (Option A, part A) avoids that re-index entirely.
        """
        recreated: dict[str, str] = {}
        for sid, raw in before.items():
            after_raw = after.get(sid)
            if after_raw is None:
                new_shared_id = await self._entity_repository.create_raw(raw)
                scope.add(new_shared_id)
                recreated[sid] = new_shared_id
                logger.info("re-created deleted dummy (old={} new={})", sid, new_shared_id)
            elif entity_unchanged(raw, after_raw):
                logger.debug("skipping no-op revert for unchanged dummy {}", sid)
            else:
                await self._entity_repository.save_raw(raw)
        logger.info("reverted {} original dummies to before-state", len(before))
        return recreated

    async def _snapshot_raws_optional_mapped(
        self, old_ids: list[str], recreated: dict[str, str]
    ) -> dict[str, dict[str, Any] | None]:
        """Post-revert snapshot keyed by **old** id, fetching re-created entities by
        their **new** id (so a re-created original is compared, not ``None``).

        Uses :func:`resolve_recreated_fetch_ids` (pure) to pick the fetch id per
        original: the re-created new id if revert re-created it, else the old id.
        A fetch that raises (the entity is gone - e.g. revert failed to restore a
        deleted original) maps to ``None``, which the restore-equality check flags
        as a mismatch.
        """
        out: dict[str, dict[str, Any] | None] = {}
        for old_id, fetch_id in resolve_recreated_fetch_ids(old_ids, recreated):
            try:
                out[old_id] = await self._entity_repository.get_raw_by_shared_id(fetch_id, self._language)
            except Exception:  # noqa: BLE001 — gone-by-revert-failure is expected, map to None
                out[old_id] = None
        return out

    async def _wait_for_es_fresh(self, targets: dict[str, int]) -> FreshnessResult | None:
        """Poll ES until each ``shared_id``'s ``editDate`` reaches its target, or timeout.

        Returns ``None`` when no :class:`SearchProbePort` is wired (the settle is
        a no-op - backward-compatible) or when ``targets`` is empty. Otherwise
        probes each id's ES ``editDate`` (``/api/v2/search?filter[sharedId]=<id>
        &fields[]=editDate``) and sleeps ``ES_SETTLE_POLL_INTERVAL_MS`` between
        rounds until every id is fresh (``editDate >= target``) or
        ``ES_SETTLE_TIMEOUT_MS`` elapses (``timed_out``). A target of ``0`` reduces
        to "visible" (any positive editDate qualifies).

        ``editDate`` is bumped on every Uwazi save (server-managed), so it is a
        monotonic "which version is refreshed" signal: once the ES ``editDate``
        reaches the latest Mongo ``editDate`` seen for a sharedId, the index has
        refreshed that latest version, so the subsequent ``deleteByQuery`` snapshots
        it (no version conflict) and removes it cleanly.

        Not unit-tested (I/O); the pure assembly is :func:`build_freshness_result`.
        """
        if self._search_probe is None or not targets:
            return None
        deadline = time.monotonic() + ES_SETTLE_TIMEOUT_MS / 1000.0
        interval = ES_SETTLE_POLL_INTERVAL_MS / 1000.0
        observed: dict[str, int | None] = {}
        timed_out = False
        while True:
            observed = {}
            for sid in targets:
                observed[sid] = await self._search_probe.shared_id_edit_date(sid, self._language)
            if all(observed[sid] is not None and observed[sid] >= targets[sid] for sid in targets):
                break
            if time.monotonic() >= deadline:
                timed_out = True
                break
            await asyncio.sleep(interval)
        result = build_freshness_result(targets, observed, timed_out)
        if result.all_fresh:
            logger.info("ES settle ok: {}/{} fresh", len(result.fresh_ids), len(result.expected_ids))
        else:
            logger.warning(
                "ES settle not fresh: {}/{}, pending={}",
                len(result.fresh_ids),
                len(result.expected_ids),
                result.pending_ids,
            )
        return result

    async def _delete_all(self, shared_ids: list[str]) -> None:
        """Delete every dummy (originals + script-created) — the §2.7 cleanup guarantee."""
        if not shared_ids:
            return
        await self._entity_api.delete_entities_by_shared_ids(shared_ids)
        logger.info("deleted {} dummy entities", len(shared_ids))
