from abc import ABC, abstractmethod

from uwazi_admin_agent.domain.manifest import MigrationManifest, RunStatus
from uwazi_admin_agent.domain.snapshot import EntitySnapshot


class BackupStorePort(ABC):
    """Filesystem persistence for snapshots and manifests (§5.3).

    Pure filesystem - no network. Sync: filesystem ops are fast; async use cases
    call these directly without ``await``. Snapshots are keyed by
    ``(run_id, shared_id)``; the revert builder's per-run snapshot loader is a
    partial application of ``load_snapshot``.
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
    def list_runs(self) -> list[str]:
        """List the run ids known to the store."""
        ...
