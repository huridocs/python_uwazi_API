from abc import ABC, abstractmethod

from uwazi_admin_agent.domain.manifest import MigrationManifest, RunStatus
from uwazi_admin_agent.domain.snapshot import EntitySnapshot


class BackupStorePort(ABC):
    """Filesystem persistence for snapshots, manifests, and captured file bytes (§5.3).

    Pure filesystem - no network. Sync: filesystem ops are fast; async use cases
    call these directly without ``await``. Snapshots are keyed by
    ``(run_id, shared_id)``; the revert builder's per-run snapshot loader is a
    partial application of ``load_snapshot``. Captured file bytes (delete-revert
    file restore) are keyed by ``(run_id, shared_id, file_id)`` as parallel
    binary artifacts — the snapshot's :class:`FileRef` list is the metadata
    manifest of which files were captured; the bytes live here.
    """

    @abstractmethod
    def save_snapshot(self, run_id: str, snapshot: EntitySnapshot) -> None:
        """Persist one entity's raw snapshot for a run."""
        ...

    @abstractmethod
    def load_snapshot(self, run_id: str, shared_id: str) -> EntitySnapshot:
        """Load one entity's snapshot for a run (raises if absent - no silent skip)."""
        ...

    @abstractmethod
    def save_file_bytes(self, run_id: str, shared_id: str, file_id: str, data: bytes) -> None:
        """Persist one captured file's bytes for a run (delete-revert file restore).

        Keyed by ``(run_id, shared_id, file_id)`` — the snapshot's :class:`FileRef`
        list is the metadata manifest; the bytes live as a parallel binary artifact.
        """
        ...

    @abstractmethod
    def load_file_bytes(self, run_id: str, shared_id: str, file_id: str) -> bytes:
        """Load one captured file's bytes (raises if absent - no silent skip)."""
        ...

    @abstractmethod
    def save_manifest(self, run_id: str, manifest: MigrationManifest) -> None:
        """Persist a run's manifest."""
        ...

    @abstractmethod
    def load_manifest(self, run_id: str) -> MigrationManifest:
        """Load a run's manifest."""
        ...

    @abstractmethod
    def update_status(self, run_id: str, status: RunStatus) -> None:
        """Update a run's status on its persisted manifest."""
        ...

    @abstractmethod
    def clear_run(self, run_id: str) -> None:
        """Wipe a run's persisted snapshots + captured file bytes (the manifest is preserved).

        Called on re-execute so stale snapshots and file bytes from a previous
        execution are removed before the intercept writes fresh ones. The manifest
        file is kept - it still carries ``prompt``/``script``/``created_at``; the
        touch-set lists are cleared separately via :meth:`MigrationManifest.reset_touch_set`.
        """
        ...

    @abstractmethod
    def list_runs(self) -> list[str]:
        """List the run ids known to the store."""
        ...
