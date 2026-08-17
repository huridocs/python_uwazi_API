"""``uwazi-admin-agent inspect-run`` — print a run's manifest summary (Phase 5).

Pure filesystem: loads the persisted ``manifest.json`` for ``run_name`` and
prints status + touch-set counts + the originating prompt + the script path.
No network, no LLM. Exit 0 on success, 2 if the run has no manifest yet
(``generate`` not run).

The rendering is split into a pure :func:`render_manifest_summary` so it is
unit-testable with a literal manifest (no FS, no mocks) per the testing policy;
:func:`run_inspect_run` is the thin filesystem-loading driver.
"""

from __future__ import annotations

from pathlib import Path

from uwazi_admin_agent.adapters.script_emitter import SCRIPT_FILENAME
from uwazi_admin_agent.configuration import RUNS_PATH
from uwazi_admin_agent.domain.manifest import MigrationManifest
from uwazi_admin_agent.drivers.runtime import build_backup_store


def run_inspect_run(run_name: str) -> int:
    """Print the manifest summary for ``run_name``; return an exit code."""
    store = build_backup_store()
    try:
        manifest = store.load_manifest(run_name)
    except FileNotFoundError:
        print(f"error: no manifest for run {run_name!r} (run `generate` first)", flush=True)
        return 2

    script_path = RUNS_PATH / run_name / SCRIPT_FILENAME
    print(render_manifest_summary(manifest, run_name, script_path))
    return 0


def render_manifest_summary(manifest: MigrationManifest, run_name: str, script_path: Path) -> str:
    """Render a multi-line, operator-readable summary of ``manifest``."""
    return "\n".join(
        [
            f"run: {run_name}",
            f"status: {manifest.status.value}",
            f"created_at: {manifest.created_at.isoformat()}",
            f"modified: {len(manifest.modified)}",
            f"deleted: {len(manifest.deleted)}",
            f"created: {len(manifest.created)}",
            f"rewired: {len(manifest.rewired)}",
            f"script: {script_path}",
            f"prompt: {manifest.prompt}",
        ]
    )
