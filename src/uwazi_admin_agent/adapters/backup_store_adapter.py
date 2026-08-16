"""Filesystem persistence for snapshots and manifests (§5 Phase 4).

Layout::

    <root>/<run_id>/manifest.json
    <root>/<run_id>/snapshots/<safe_shared_id>.json

Pure filesystem - no network. Snapshots are keyed by ``(run_id, shared_id)``.
"""

import re
from pathlib import Path
from typing import override

from loguru import logger

from uwazi_admin_agent.domain.manifest import MigrationManifest, RunStatus
from uwazi_admin_agent.domain.snapshot import EntitySnapshot
from uwazi_admin_agent.ports.backup_store_port import BackupStorePort

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def _safe_name(shared_id: str) -> str:
    """Filesystem-safe snapshot filename for a sharedId (§8: sharedIds are
    expected to be safe, but sanitize defensively so a stray char can't break
    the store)."""
    return _UNSAFE.sub("_", shared_id) + ".json"


class FilesystemBackupStore(BackupStorePort):
    """On-disk snapshot + manifest store."""

    def __init__(self, root: Path) -> None:
        self._root: Path = Path(root)

    # --- snapshots -----------------------------------------------------------

    @override
    def save_snapshot(self, run_id: str, snapshot: EntitySnapshot) -> None:
        snapshots_dir = self._run_dir(run_id) / "snapshots"
        snapshots_dir.mkdir(parents=True, exist_ok=True)
        path = snapshots_dir / _safe_name(snapshot.shared_id)
        _ = path.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
        logger.debug("snapshot saved run={} sharedId={}", run_id, snapshot.shared_id)

    @override
    def load_snapshot(self, run_id: str, shared_id: str) -> EntitySnapshot:
        path = self._run_dir(run_id) / "snapshots" / _safe_name(shared_id)
        if not path.exists():
            raise FileNotFoundError(f"No snapshot for run={run_id} sharedId={shared_id}")
        return EntitySnapshot.model_validate_json(path.read_text(encoding="utf-8"))

    # --- manifests -----------------------------------------------------------

    @override
    def save_manifest(self, run_id: str, manifest: MigrationManifest) -> None:
        run_dir = self._run_dir(run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        path = run_dir / "manifest.json"
        _ = path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
        logger.debug("manifest saved run={}", run_id)

    @override
    def load_manifest(self, run_id: str) -> MigrationManifest:
        path = self._run_dir(run_id) / "manifest.json"
        if not path.exists():
            raise FileNotFoundError(f"No manifest for run={run_id}")
        return MigrationManifest.model_validate_json(path.read_text(encoding="utf-8"))

    @override
    def update_status(self, run_id: str, status: RunStatus) -> None:
        manifest = self.load_manifest(run_id)
        manifest.status = status
        self.save_manifest(run_id, manifest)
        logger.debug("status updated run={} status={}", run_id, status)

    # --- listing -------------------------------------------------------------

    @override
    def list_runs(self) -> list[str]:
        if not self._root.exists():
            return []
        runs = [child.name for child in self._root.iterdir() if (child / "manifest.json").exists()]
        return sorted(runs)

    # --- helpers -------------------------------------------------------------

    def _run_dir(self, run_id: str) -> Path:
        return self._root / run_id
