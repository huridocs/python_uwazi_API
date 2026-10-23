"""The one-at-a-time gate for operations that write to Uwazi, plus their progress.

The review page's Run / Revert / refresh, the task page's whole-task revert and
the app's generation are long passes over the SAME Uwazi instance (and the same
SQLite review store), so two of them must never overlap: two manifests would
race to record the same entity's first-touch snapshot, cache invalidation in
the middle of a scan would hide live data, and one pass's row-status writes
would read the other's half-committed state. This module is that serializer:
one process-global slot over all clients and pages — at most one Uwazi-write
operation holds it, a second attempt is refused, and the slot is freed only
when its owner finishes (an ownership match guards the release).

Progress: the owner reports ``(done, total)`` ticks from the worker thread that
performs the writes (``set_progress`` is thread-safe; the unit is the gate's
KIND — suboperations for a Run, entities for a Revert). Pages poll
``snapshot`` with a fast timer and reissue ONE grouped Quasar toast —
"Running — 13/1230 suboperations…" with a filling bar — so an operator sees a
live fraction instead of a silent minute of work.

No NiceGUI imports: the registry, the gate itself and the display helpers are
pure and unit-testable offline (see ``tests/test_operation_gate.py``).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class GateState:
    """The gate's current holder + its latest progress tick (immutable snapshot).

    Mutating functions replace this instance; readers for the UI work with
    ``snapshot()`` to get a complete and stable state.
    """

    kind: str
    run_id: str
    done: int
    total: int
    started_at: datetime


# The gated kinds, with the operator-facing words each one is shown as.
KIND_VERBS: dict[str, str] = {
    "run": "Running",
    "revert": "Reverting",
    "refresh": "Refreshing the table",
    "creating": "Generating",
}

# The unit whose count the progress fraction counts. A Run writes SUBOPERATIONS
# (the table's per-property rows: 1230 rows = 1230 units), a Revert restores
# ENTITIES (one backup per entity, whatever languages it wrote).
KIND_UNITS: dict[str, str] = {
    "run": "suboperations",
    "revert": "entity",
    "refresh": "",
    "creating": "",
}


def progress_text(done: int, total: int) -> str:
    """The '13/1230' fraction (pure), or '…' while the total is unknown."""
    if total <= 0:
        return "…"
    done = max(0, min(done, total))
    return f"{done}/{total}"


def active_text(state: GateState | None) -> str:
    """The live toast's ONE line for what is running (pure)."""
    if state is None:
        return "Uwazi operation in progress…"
    verb = KIND_VERBS.get(state.kind, state.kind)
    unit = KIND_UNITS.get(state.kind, "")
    progress = progress_text(state.done, state.total)
    if unit:
        return f"{verb} — {progress} {unit}…"
    return f"{verb}…"


def busy_text(state: GateState | None) -> str:
    """The refusal toast's text when a second operation is attempted (pure)."""
    if state is None:
        return "Busy — another Uwazi operation is in progress. Wait until it finishes."
    name = f" on run {state.run_id!r}" if state.run_id else ""
    return f"Busy — {active_text(state).lower().capitalize()}{name}. Wait until it finishes."


_LOCK = threading.Lock()
_state: GateState | None = None


def try_acquire(kind: str, run_id: str) -> bool:
    """Claim the single Uwazi-write slot (False when another op already holds it).

    The claim check and the write are ATOMIC under one lock: exactly one
    caller wins; every other gets False and reads its holder through
    :func:`snapshot`.
    """
    global _state
    with _LOCK:
        if _state is not None:
            return False
        _state = GateState(
            kind=kind,
            run_id=run_id,
            done=0,
            total=0,
            started_at=datetime.now(timezone.utc),
        )
    return True


def release(kind: str | None = None, run_id: str | None = None) -> None:
    """Drop the claim when the caller still owns it (a mismatch keeps it).

    ``kind``/``run_id`` are the ownership check: an orphaned finish (a slow
    task releasing after its claim was already taken over-not possible while
    the gate is held, but cheap to guard) must not free another op's slot.
    """
    global _state
    with _LOCK:
        if _state is None:
            return
        if kind is not None and _state.kind != kind:
            return
        if run_id is not None and _state.run_id != run_id:
            return
        _state = None


def snapshot() -> GateState | None:
    """The current holder + progress, or None when idle (a full view)."""
    with _LOCK:
        return _state


def set_progress(done: int, total: int) -> None:
    """Record one progress tick (thread-safe; called from worker threads).

    ``total`` 0 means the unit count is still unknown (an indeterminate pass):
    the toast then shows '…' and an infinite bar. Ints carry the fraction; a
    holder-less write (a late report from a released op) is ignored.
    """
    global _state
    with _LOCK:
        if _state is None:
            return
        _state = GateState(
            kind=_state.kind,
            run_id=_state.run_id,
            done=int(done),
            total=int(total),
            started_at=_state.started_at,
        )


__all__ = [
    "GateState",
    "KIND_UNITS",
    "KIND_VERBS",
    "active_text",
    "busy_text",
    "progress_text",
    "release",
    "set_progress",
    "snapshot",
    "try_acquire",
]
