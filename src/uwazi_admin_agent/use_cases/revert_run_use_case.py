"""Revert a run by loading the manifest + snapshots and restoring exactly (§2.3, §2.6, Phase 4).

The production revert path: load the persisted manifest, call the pure
:func:`build_revert_actions` to get the ordered action list, and execute each
action via :class:`EntityRepositoryPort` (``save_raw`` for entity/relationship
restores, ``delete_by_shared_id`` for created-entity deletions). The ordering
(relationships → modified → deleted → delete-created-last) is guaranteed by
the builder; this use case is a thin orchestrator.

Testable with an in-memory repo + in-memory backup store (the DoD's "with an
in-memory repo").
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from uwazi_admin_agent.domain.manifest import RunStatus
from uwazi_admin_agent.domain.revert import (
    DeleteEntityAction,
    RestoreEntityAction,
    RestoreRelationshipAction,
    build_revert_actions,
)
from uwazi_admin_agent.ports.backup_store_port import BackupStorePort
from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort


class RevertRunUseCase:
    """Revert a run: load manifest, build actions, execute restores/deletes in order."""

    def __init__(
        self,
        entity_repository: EntityRepositoryPort,
        backup_store: BackupStorePort,
    ) -> None:
        self._entity_repository: EntityRepositoryPort = entity_repository
        self._backup_store: BackupStorePort = backup_store

    async def revert(self, run_id: str) -> None:
        """Revert run ``run_id`` by restoring every backed-up entity and deleting created ones."""
        manifest = self._backup_store.load_manifest(run_id)
        actions = build_revert_actions(manifest, lambda sid: self._backup_store.load_snapshot(run_id, sid))

        for action in actions:
            await self._execute_action(action)

        self._backup_store.update_status(run_id, RunStatus.REVERTED)
        logger.info(
            "revert done run={} actions={} modified={} deleted={} created={}",
            run_id,
            len(actions),
            len(manifest.modified),
            len(manifest.deleted),
            len(manifest.created),
        )

    async def _execute_action(self, action: Any) -> None:
        """Dispatch one revert action to the entity repository."""
        if isinstance(action, RestoreRelationshipAction):
            await self._restore_relationship(action)
        elif isinstance(action, RestoreEntityAction):
            await self._entity_repository.save_raw(action.snapshot.raw)
            logger.debug("revert: restored entity sharedId={}", action.snapshot.shared_id)
        elif isinstance(action, DeleteEntityAction):
            await self._entity_repository.delete_by_shared_id(action.shared_id)
            logger.debug("revert: deleted created entity sharedId={}", action.shared_id)

    async def _restore_relationship(self, action: RestoreRelationshipAction) -> None:
        """Fetch the entity's current raw, patch the relationship field, save."""
        raw = await self._entity_repository.get_raw_by_shared_id(action.entity.shared_id, action.entity.language)
        raw[action.property_name] = action.before
        await self._entity_repository.save_raw(raw)
        logger.debug("revert: restored relationship sharedId={} property={}", action.entity.shared_id, action.property_name)
