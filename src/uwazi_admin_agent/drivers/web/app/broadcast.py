"""Runs-table broadcast/refresh helpers shared by the web app modules.

These functions were extracted from ``runs_table`` so the dialogs, wizard and
state modules can refresh the table without importing the table module (which
would create an import cycle). The behavior is unchanged.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any

from nicegui import app, context

from uwazi_admin_agent.domain.execute_gate import ExecuteRefusedError
from uwazi_admin_agent.domain.revert_gate import RevertRefusedError
from uwazi_admin_agent.drivers.web import operation_gate
from uwazi_admin_agent.drivers.web.run_service import (
    GenerateError,
    RevertVerificationError,
    RunSummary,
    list_runs,
)
from uwazi_admin_agent.drivers.web.session import (
    client_is_active,
    page_instance_url,
    session_is_active,
)

from .state import (
    _broadcast_notify,
    _notify_error,
    _run_in_flight,
    _run_is_busy,
    _run_stage,
)


def _summary_to_row(run: RunSummary, instance: str) -> dict[str, Any]:
    status = _run_in_flight(instance, run.run_id)
    return {
        "id": run.run_id,
        "name": run.run_id,
        "status": status or run.status.value,
        # The live work stage ("Running the extraction benchmark…"). Only an
        # in-flight claim has one; a finished run keeps a blank caption.
        "stage": _run_stage(instance, run.run_id) if status else "",
        "created": run.created_at.strftime("%Y-%m-%d %H:%M"),
        # The numeric key the table sorts on (creation, newest first). The
        # display column keeps the human timestamp; sorting the TEXT would
        # order "2026-09-01 10:00" before "2026-10-01 09:00" wrongly only by
        # luck, and would not survive a locale change.
        "created_ts": _timestamp(run.created_at),
        "last_executed": run.last_executed_at.strftime("%Y-%m-%d %H:%M") if run.last_executed_at else "—",
        "modified": run.modified,
        "deleted": run.deleted,
        "created_count": run.created,
        "rewired": run.rewired,
        "error": bool(run.error),
        "busy": _run_is_busy(instance, run.run_id),
    }


def _timestamp(value: datetime) -> float:
    """One creation timestamp as a sortable epoch (pure)."""
    return value.timestamp()


def _creating_to_row(name: str, stage: str = "") -> dict[str, Any]:
    """A placeholder row for a run whose generation is in flight."""
    return {
        "id": name,
        "name": name,
        "status": "creating",
        "stage": stage,
        "created": "—",
        # A task being generated is the newest one there is.
        "created_ts": time.time(),
        "last_executed": "—",
        "modified": 0,
        "deleted": 0,
        "created_count": 0,
        "rewired": 0,
        "error": False,
        "busy": True,
    }


def _run_rows(instance: str | None = None) -> list[dict[str, Any]]:
    """Compute one client's rows (persisted runs + in-flight placeholders).

    Both lists are scoped to ``instance`` (the client's session) AND to claims
    on that instance, so another instance's generation never appears here, nor
    does this instance's activity appear in another instance's table. A page
    whose session logged out or switched instance in another tab reads NO rows:
    an unbound page must not keep serving the previous instance's runs.
    """
    instance = page_instance_url() if instance is None else instance
    if not session_is_active(instance):
        return []
    runs = list_runs(instance)
    persisted_ids = {r.run_id for r in runs}
    rows = [_summary_to_row(r, instance) for r in runs]
    # Merge in runs whose generation is still in flight (not yet persisted).
    for claim in operation_gate.run_claims(instance):
        if claim.kind == "creating" and claim.run_id not in persisted_ids:
            rows.append(_creating_to_row(claim.run_id, claim.note))
    return rows


def _refresh_rows_client(instance: str | None = None) -> None:
    """Push fresh rows + per-row busy state into this client's live page.

    The tasks page never blocks globally: another run (or another instance)
    being written is not a reason to disable this table. Only a row whose OWN
    run is busy is disabled, because operating on it would genuinely conflict.
    """
    table = getattr(context.client, "_runs_table", None)
    if table is None or table.is_deleted:
        return
    instance = page_instance_url() if instance is None else instance
    if not session_is_active(instance):
        # Logged out (in this tab or another one): stop reading and stop showing
        # the previous instance's rows.
        if table.rows:
            table.rows = []
            table.selected = []
            table.update()
        return
    table.rows = _run_rows(instance)
    _prune_table_selection(table)
    table.update()
    _sync_bulk_delete_button(instance)


def _prune_table_selection(table: Any) -> None:
    """Drop selected rows that are no longer in the table.

    Reassigning ``table.rows`` does not clear the checkbox selection: Quasar
    keeps rendering ``props.selected``, so a deleted run stayed counted in the
    footer ("2 records selected") even though its row was gone. The selected
    set is the client's ``_task_selection`` plus Quasar's own ``selected`` prop;
    both are pruned against the rows that still exist.
    """
    rows = table.rows or []
    visible = {str(row.get("id")) for row in rows if isinstance(row, dict)}
    current = [row for row in (table.selected or []) if isinstance(row, dict) and str(row.get("id")) in visible]
    if len(current) != len(table.selected or []):
        table.selected = current
    selection = getattr(context.client, "_task_selection", None)
    if selection is not None:
        context.client._task_selection = [run_id for run_id in selection if run_id in visible]


def _broadcast_rows(instance: str) -> None:
    """Refresh the runs table of clients bound to ONE instance (background-safe)."""
    if not instance:
        return
    for client in app.clients():
        if not client_is_active(client, instance):
            continue
        if getattr(client, "_runs_table", None) is None:
            continue
        with client:
            _refresh_rows_client(instance)


def _sync_bulk_delete_button(instance: str | None = None) -> None:
    """Enable bulk Delete iff rows are checked and none of them is busy.

    Called by BOTH the selection handler and the 5s refresh. The handler used to
    only record the ids, so the button stayed Quasar-disabled — a disabled
    ``q-btn`` swallows clicks — until the next timer tick, which is why Delete
    felt unclickable for up to 5s after checking a row.
    """
    delete_btn = getattr(context.client, "_delete_selected_button", None)
    if delete_btn is None or delete_btn.is_deleted:
        return
    instance = page_instance_url() if instance is None else instance
    if not session_is_active(instance):
        delete_btn.set_enabled(False)
        return
    selection = getattr(context.client, "_task_selection", [])
    busy = any(_run_is_busy(instance, run_id) for run_id in selection)
    delete_btn.set_enabled(bool(selection) and not busy)


async def _run_async(
    coro: Any,
    success_msg: str,
    instance: str,
    run_id: str | None = None,
    claim_kind: str | None = None,
) -> None:
    """Await a coroutine operation, notify its instance, then refresh its table.

    The service coroutines (execute/rollback) perform *synchronous* HTTP via the
    ``requests``-based Uwazi client, which would block the event loop for the
    whole operation — stalling the outbox (so the in-flight badge and table
    refresh never reach the browser) and the websocket heartbeat (connection
    drops). Running the coroutine on a worker thread keeps the loop free.
    """
    try:
        await asyncio.to_thread(asyncio.run, coro)
        _broadcast_notify(instance, success_msg, type="positive")
    except GenerateError as exc:
        _notify_error(instance, "Generation failed", str(exc))
    except RevertVerificationError as exc:
        _notify_error(instance, "Revert completed but verification found mismatches", str(exc))
    except (ExecuteRefusedError, RevertRefusedError) as exc:
        _notify_error(instance, "Refused", str(exc), type_="warning")
    except Exception as exc:  # noqa: BLE001 — surface every failure to the operator
        _notify_error(instance, "Error", str(exc))
    finally:
        if run_id is not None:
            operation_gate.release_run(instance, run_id, claim_kind)
        _broadcast_rows(instance)
