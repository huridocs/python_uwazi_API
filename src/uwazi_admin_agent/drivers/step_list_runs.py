"""``uwazi-admin-agent list-runs`` — list run ids known to the backup store (Phase 5).

Pure filesystem: ``FilesystemBackupStore(RUNS_PATH).list_runs()`` returns run
ids (folders with a ``manifest.json``), sorted, or ``[]`` when the runs root
does not exist. Prints one id per line. This is the Phase-5 DoD step — it must
run without error against an empty runs root.
"""

from __future__ import annotations

from uwazi_admin_agent.drivers.runtime import build_backup_store


def run_list_runs() -> int:
    """List run ids to stdout; return 0. Empty root prints nothing (not an error)."""
    store = build_backup_store()
    run_ids = store.list_runs()
    for run_id in run_ids:
        print(run_id)
    return 0
