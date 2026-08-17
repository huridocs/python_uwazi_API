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

Not unit-tested (it needs the real instance); the DoD covers the pure parts only.
"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from uwazi_admin_agent.domain.validation_result import build_validation_outcome
from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort
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
    ) -> None:
        self._entity_api: EntityApiPort = entity_api
        self._relationship_api: RelationshipApiPort | None = relationship_api
        self._entity_repository: EntityRepositoryPort = entity_repository
        self._language: str = language

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

        try:
            created_ids = await self._create_dummies(dummy_spec)
            scope.update(created_ids)
            before = await self._snapshot_raws(created_ids)
            dummy_entities = await self._fetch_dummy_entities(created_ids)

            script_result, script_error, script_created_ids = await self._run_script(script, scope, dummy_entities)
            after = await self._snapshot_raws_optional(scope)

            if script_error is None:
                await self._revert_originals(before)
                post_revert = await self._snapshot_raws_optional(list(before.keys()))
        except Exception as exc:  # noqa: BLE001 — harness-level error must not escape without cleanup
            script_error = script_error or f"Harness error: {type(exc).__name__}: {exc}"
            logger.error("dummy harness error: {}", exc)
        finally:
            try:
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

    async def _revert_originals(self, before: dict[str, dict[str, Any]]) -> None:
        """Restore each original dummy to its exact before raw (full raw, incl. relations)."""
        for sid, raw in before.items():
            await self._entity_repository.save_raw(raw)
        logger.info("reverted {} original dummies to before-state", len(before))

    async def _delete_all(self, shared_ids: list[str]) -> None:
        """Delete every dummy (originals + script-created) — the §2.7 cleanup guarantee."""
        if not shared_ids:
            return
        await self._entity_api.delete_entities_by_shared_ids(shared_ids)
        logger.info("deleted {} dummy entities", len(shared_ids))
