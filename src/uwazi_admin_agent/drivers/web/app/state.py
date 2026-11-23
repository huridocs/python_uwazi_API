"""Web driver module split out of the original ``app`` (behavior preserved)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from loguru import logger
from nicegui import app, ui

from uwazi_admin_agent.drivers.web import operation_gate

# Import for its side effect: the ``@ui.page("/review/{run_id}")`` route registers
# on import, so the runs table's Review action can navigate to it.
from uwazi_admin_agent.drivers.web import review_page as _review_page_module  # noqa: F401
from uwazi_admin_agent.drivers.web import task_page as _task_page_module  # noqa: F401
from uwazi_admin_agent.drivers.web.session import client_is_active, session_is_active

_STATUS_COLORS: dict[str, str] = {
    "creating": "blue-grey",
    "planned": "grey",
    "snapshotted": "orange",
    "running": "blue",
    "reverting": "purple",
    "executed": "green",
    "verified": "blue",
    "reverted": "indigo",
    "failed": "red",
    "generation_failed": "deep-orange",
}

_ABORT: object = object()

_CLAIM_STATUS: dict[str, str] = {
    "creating": "creating",
    "run": "running",
    "revert": "reverting",
}

_IN_FLIGHT_JS = "['creating', 'running', 'reverting'].includes(props.row.status)"

_STATUS_COLOR_JS = "{" + ", ".join(f"'{k}': '{v}'" for k, v in _STATUS_COLORS.items()) + "}"

_EXAMPLE_TASKS = [
    (
        "Find missing supporting files",
        "Show entities from template DOCUMENT and Document Status to Adopted that has no supporting files",
    ),
    (
        "Extract data from attachments",
        "Fill a property from the attached HTML file",
    ),
    (
        "Merge multilingual duplicates",
        "Merge entities holding same PDF in different languages",
    ),
]

# The web app is a PACKAGE (``drivers/web/app/``) whose static assets stayed at
# the parent ``drivers/web/static`` when the original ``app.py`` was split, so
# the directory is two levels up from this module. Pointing at the package
# directory mounted an empty tree and every pdf.js asset answered 404.
_STATIC_DIR = Path(__file__).resolve().parents[1] / "static"


def _run_in_flight(instance: str, run_id: str) -> str | None:
    """The UI-only status of a run, or None when it is free.

    The Uwazi-write gate is the authority for Run/Revert; a local claim covers
    generation/sync/rename/delete. Both are scoped to the client's instance, so
    another instance's activity never marks this table busy.
    """
    gate = operation_gate.snapshot(instance)
    if gate is not None and gate.run_id == run_id:
        status = _CLAIM_STATUS.get(gate.kind)
        if status is not None:
            return status
    claim = operation_gate.run_claim(instance, run_id)
    if claim is None:
        return None
    return _CLAIM_STATUS.get(claim.kind)


def _run_is_busy(instance: str, run_id: str) -> bool:
    """True when THIS run has an operation that would conflict with another."""
    gate = operation_gate.snapshot(instance)
    if gate is not None and gate.run_id == run_id:
        return True
    return operation_gate.run_claim(instance, run_id) is not None


def _busy_run_text(instance: str, run_id: str) -> str:
    """Why a run cannot start another local operation right now (for a refusal)."""
    gate = operation_gate.snapshot(instance)
    if gate is not None and gate.run_id == run_id:
        return operation_gate.busy_text(gate)
    return operation_gate.run_claim_text(operation_gate.run_claim(instance, run_id))


def _bulk_label(count: int, kind: str) -> str:
    """The refusal text for a bulk action over ``count`` tasks (pure)."""
    action = {"deleting": "being deleted"}.get(kind, kind)
    verb = "is" if count == 1 else "are"
    noun = "task" if count == 1 else "tasks"
    return f"{count} {noun} {verb} {action}"


def _try_claim_bulk(instance: str, run_ids: list[str], kind: str) -> bool:
    """Claim every selected run ATOMICALLY for one bulk local action.

    Different runs stay independent: only a selected run that is already busy
    refuses the whole action (and nothing is claimed in that case).
    """
    return operation_gate.try_claim_runs(instance, run_ids, kind)


def _release_bulk_claim(instance: str, run_ids: list[str], kind: str = "deleting") -> None:
    """Release the bulk claims owned by this action (never another operation)."""
    operation_gate.release_runs(instance, run_ids, kind)
    from .broadcast import _broadcast_rows  # local: state is a lower layer than broadcast

    _broadcast_rows(instance)


def _session_ended(instance: str) -> bool:
    """True — after telling the operator — when this page no longer owns ``instance``.

    An open tab whose session logged out (or switched instance elsewhere) still
    has its buttons; acting on the captured instance would read a tree this
    login no longer owns. Every instance-bound action starts with this check.
    """
    if session_is_active(instance):
        return False
    ui.notify("Your session ended or switched instance — log in again", type="warning")
    return True


def _broadcast_notify(instance: str, message: str, type: str = "positive", **kwargs: Any) -> None:
    """Send a notification to clients bound to ONE instance (background-safe).

    ``ui.notify`` requires a client context, which background tasks lack. This
    iterates the connected clients but only reaches sessions whose page was
    built for ``instance``: another instance's operator must never see this
    action, and an unregistered (login-page) client receives nothing.
    """
    if not instance:
        return
    options = {"message": str(message), "type": type, **kwargs}
    for client in app.clients():
        if not client_is_active(client, instance):
            continue
        client.outbox.enqueue_message("notify", options, client.id)


def _notify_error(instance: str, title: str, detail: str, type_: str = "negative") -> None:
    """Short headline toast for ONE instance; the full detail lives in its dialog.

    Also LOGS to the process log (stderr, i.e. the ``docker logs`` stream). Every
    failure toast is raised from inside an ``except`` block of a NiceGUI
    background task, whose exceptions never reach the uvicorn error handler — so
    without this the container terminal stayed silent and the only trace of a
    failed generation was a toast that vanished with the page.
    ``logger.exception`` attaches the traceback while one is active, which is what
    makes the failure debuggable from the terminal alone.

    The log is deliberately NOT mirrored into the browser: a process-wide ring
    buffer is shared by every instance, so an in-app log viewer would show one
    instance's runs, ids and file names to operators of another.
    """
    first_line = (detail or "").strip().splitlines()[0] if (detail or "").strip() else title
    if sys.exc_info()[0] is not None:
        logger.exception("{}: {}", title, detail)
    else:
        logger.error("{}: {}", title, detail)
    _broadcast_notify(instance, title, type_=type_, caption=first_line)
