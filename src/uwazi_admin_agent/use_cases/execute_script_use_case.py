"""Run a validated script against real entities with backup-intercepted CRUD (§2.2 execute step, Phase 4).

The script was proven reversible against dummies (Phase 3). This use case runs
the *same* script against real entities, but the exec namespace's write helpers
are decorated by :class:`BackupIntercept` so every modification snapshots the
raw before-state into :class:`FilesystemBackupStore` + :class:`MigrationManifest`
*before* applying (§2.4). The manifest is persisted after execution so the
revert use case can restore exactly.

Phase 6 productionizes safety on top of the Phase-4 flow:
- the **max-entities cap** is enforced mid-script (the intercept raises
  :class:`CapExceededError` after each op that grows the touch set);
- an **on-error policy** (``stop`` vs ``stop-and-revert``) decides what happens
  when the script raises — leave the partial manifest, or auto-revert it;
- every intercepted op is recorded in the run's **audit log** (via the
  :class:`AuditLogPort` injected into the intercept); a run-level
  ``execute`` audit record captures the overall outcome.

Mirrors :class:`DummyEntityHarness._run_script`'s worker-thread pattern (a
dedicated event loop for the sync CRUD helpers' ``run_until_complete`` calls).
Not unit-tested (needs ports); validated via the simulation run.
"""

import asyncio
from datetime import datetime, timezone

from loguru import logger

from uwazi_admin_agent.domain.audit_record import AuditOutcome, AuditStep, make_audit_record
from uwazi_admin_agent.domain.execute_gate import ExecuteRefusedError, decide_execute_gate
from uwazi_admin_agent.domain.manifest import MigrationManifest, RunStatus
from uwazi_admin_agent.domain.on_error_policy import OnErrorPolicy, should_auto_revert
from uwazi_admin_agent.ports.audit_log_port import AuditLogPort
from uwazi_admin_agent.ports.backup_store_port import BackupStorePort
from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort
from uwazi_admin_agent.ports.file_repository_port import FileRepositoryPort
from uwazi_admin_agent.use_cases.backup_intercept import BackupIntercept
from uwazi_admin_agent.use_cases.revert_run_use_case import RevertRunUseCase
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
        audit_log: AuditLogPort | None = None,
        cap: int = 1000,
        revert_use_case: RevertRunUseCase | None = None,
        file_repository: FileRepositoryPort | None = None,
    ) -> None:
        self._entity_api: EntityApiPort = entity_api
        self._relationship_api: RelationshipApiPort | None = relationship_api
        self._entity_repository: EntityRepositoryPort = entity_repository
        self._backup_store: BackupStorePort = backup_store
        self._audit_log: AuditLogPort | None = audit_log
        self._cap: int = cap
        self._revert_use_case: RevertRunUseCase | None = revert_use_case
        self._file_repository: FileRepositoryPort | None = file_repository

    async def execute(
        self,
        script: str,
        manifest: MigrationManifest,
        run_id: str,
        language: str = "en",
        on_error_policy: OnErrorPolicy = OnErrorPolicy.STOP,
    ) -> MigrationManifest:
        """Run ``script`` against real entities; return the populated manifest.

        The manifest is populated by the intercept as the script runs, then
        persisted. On script error (incl. :class:`CapExceededError`) the manifest
        (with whatever was backed up before the error) is still persisted so a
        partial revert is possible, and the status is set to ``FAILED``. The
        ``on_error_policy`` then decides whether to leave the partial run (the
        operator reverts later) or auto-revert it now via :class:`RevertRunUseCase`.
        ``STOP_AND_REVERT`` requires a ``revert_use_case`` to have been injected.
        """
        if on_error_policy == OnErrorPolicy.STOP_AND_REVERT and self._revert_use_case is None:
            raise ValueError("on_error_policy=stop-and-revert requires a revert_use_case to be injected.")

        gate = decide_execute_gate(manifest.status, self._has_touch_set(manifest))
        if gate.action == "refuse":
            assert gate.reason is not None  # refused decisions always carry a reason
            raise ExecuteRefusedError(gate.reason)
        if gate.needs_reset:
            manifest.reset_touch_set()
            self._backup_store.clear_run(run_id)
            logger.info("execute: reset touch set before re-execute run={}", run_id)

        intercept = BackupIntercept(
            entity_repository=self._entity_repository,
            backup_store=self._backup_store,
            manifest=manifest,
            run_id=run_id,
            language=language,
            loop=None,  # set inside the worker thread via intercept.set_loop()
            audit_log=self._audit_log,
            cap=self._cap,
            file_repository=self._file_repository,
        )

        _result, error = await asyncio.to_thread(self._exec, script, intercept, language)
        manifest = intercept.manifest

        if error:
            manifest.last_executed_at = datetime.now(timezone.utc)
            manifest.status = RunStatus.FAILED
            manifest.error = error
            manifest.error_step = "execute"
            self._backup_store.save_manifest(run_id, manifest)
            self._emit_run(AuditOutcome.FAILURE, run_id, detail=error.splitlines()[0] if error else error)
            logger.error("execute script failed run={} error={}", run_id, error.splitlines()[0] if error else error)
            if should_auto_revert(on_error_policy, manifest):
                logger.info("on-error=stop-and-revert: auto-reverting run={}", run_id)
                assert self._revert_use_case is not None  # guarded above
                await self._revert_use_case.revert(run_id)
                return self._backup_store.load_manifest(run_id)
            raise ScriptExecutionError(error)

        manifest.last_executed_at = datetime.now(timezone.utc)
        manifest.status = RunStatus.EXECUTED
        manifest.error = None
        manifest.error_step = None
        self._backup_store.save_manifest(run_id, manifest)

        self._emit_run(AuditOutcome.SUCCESS, run_id)
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
                entity_repository=self._entity_repository,
                file_repository=self._file_repository,
            )
            return run_script_sync(script, namespace)
        finally:
            loop.close()

    def _emit_run(self, outcome: AuditOutcome, run_id: str, detail: str | None = None) -> None:
        """Append a run-level ``execute`` audit record (no-op if no audit log)."""
        if self._audit_log is None:
            return
        self._audit_log.append(
            run_id,
            make_audit_record(
                run_id=run_id,
                step=AuditStep.EXECUTE,
                op_kind="execute",
                shared_ids=[],
                outcome=outcome,
                detail=detail,
            ),
        )

    @staticmethod
    def _has_touch_set(manifest: MigrationManifest) -> bool:
        """True if the manifest already carries any touch-set entries."""
        return any((manifest.modified, manifest.deleted, manifest.created, manifest.rewired))
