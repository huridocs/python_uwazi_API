"""Run a validated script against real entities with backup-intercepted CRUD (§2.2 execute step, Phase 4).

The script was proven reversible against dummies (Phase 3). This use case runs
the *same* script against real entities, but the exec namespace's write helpers
are decorated by :class:`BackupIntercept` so every modification snapshots the
raw before-state into :class:`FilesystemBackupStore` + :class:`MigrationManifest`
*before* applying (§2.4). The manifest is persisted after execution so the
revert use case can restore exactly.

Mirrors :class:`DummyEntityHarness._run_script`'s worker-thread pattern (a
dedicated event loop for the sync CRUD helpers' ``run_until_complete`` calls).
Not unit-tested (needs ports); validated via the simulation run.
"""

from __future__ import annotations

import asyncio

from loguru import logger

from uwazi_admin_agent.domain.manifest import MigrationManifest, RunStatus
from uwazi_admin_agent.ports.backup_store_port import BackupStorePort
from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort
from uwazi_admin_agent.use_cases.backup_intercept import BackupIntercept
from uwazi_admin_agent.use_cases.script_exec_namespace import build_real_exec_namespace, run_script_sync
from uwazi_agent.ports.entity_api_port import EntityApiPort
from uwazi_agent.ports.relationship_api_port import RelationshipApiPort
from uwazi_agent.use_cases.tools.tool_call_cache import ToolCallCache


class ExecuteScriptUseCase:
    """Execute a validated script against real entities with backup by intercept."""

    def __init__(
        self,
        entity_api: EntityApiPort,
        relationship_api: RelationshipApiPort | None,
        entity_repository: EntityRepositoryPort,
        backup_store: BackupStorePort,
    ) -> None:
        self._entity_api: EntityApiPort = entity_api
        self._relationship_api: RelationshipApiPort | None = relationship_api
        self._entity_repository: EntityRepositoryPort = entity_repository
        self._backup_store: BackupStorePort = backup_store

    async def execute(
        self,
        script: str,
        manifest: MigrationManifest,
        run_id: str,
        language: str = "en",
    ) -> MigrationManifest:
        """Run ``script`` against real entities; return the populated manifest.

        The manifest is populated by the intercept as the script runs, then
        persisted. On script error the manifest (with whatever was backed up
        before the error) is still persisted so a partial revert is possible,
        and the status is set to ``FAILED``.
        """
        intercept = BackupIntercept(
            entity_repository=self._entity_repository,
            backup_store=self._backup_store,
            manifest=manifest,
            run_id=run_id,
            language=language,
            loop=None,  # set inside the worker thread via intercept.set_loop()
        )

        _result, error = await asyncio.to_thread(self._exec, script, intercept, language)
        manifest = intercept.manifest

        if error:
            manifest.status = RunStatus.FAILED
            self._backup_store.save_manifest(run_id, manifest)
            logger.error("execute script failed run={} error={}", run_id, error.splitlines()[0] if error else error)
            raise RuntimeError(f"Script execution failed: {error}")

        manifest.status = RunStatus.EXECUTED
        self._backup_store.save_manifest(run_id, manifest)
        logger.info(
            "execute script done run={} modified={} deleted={} created={} rewired={}",
            run_id,
            len(manifest.modified),
            len(manifest.deleted),
            len(manifest.created),
            len(manifest.rewired),
        )
        return manifest

    def _exec(self, script: str, intercept: BackupIntercept, language: str) -> tuple[str | None, str | None]:
        """Run the script in a worker thread with a dedicated event loop."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        intercept.set_loop(loop)
        try:
            namespace = build_real_exec_namespace(
                entity_api=self._entity_api,
                relationship_api=self._relationship_api,
                loop=loop,
                intercept=intercept,
                tool_cache=ToolCallCache(),
                default_language=language,
            )
            return run_script_sync(script, namespace)
        finally:
            loop.close()
