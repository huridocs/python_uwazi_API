"""Per-instance gates/claims for operations that touch Uwazi or a run's artifacts.

Two independent scopes, both keyed by Uwazi instance so one instance's work never
blocks another:

* The **Uwazi-write gate** (:func:`try_acquire` and friends): at most one
  operation that MUTATES Uwazi runs per instance. Two write passes over the same
  instance would race to record the same entity's first-touch snapshot, and one
  pass's row-status writes would read the other's half-committed state. The gate
  also carries live progress so the review page's toast can show
  ``"Running — 13/1230 suboperations…"``.
* **Per-run claims** (:func:`try_claim_run` and friends): local work on one run's
  folder/table (generation, table sync, rename, delete, and also the write
  operations themselves). Different runs of the same instance can generate or
  sync at the same time; only work on the SAME run is refused.

Generation and table refreshes never take the Uwazi-write gate: they do not
change Uwazi, so they must not block an operator who is applying changes (or be
blocked by one) on another run.

No NiceGUI imports: the registries and display helpers are pure and
unit-testable offline (see ``tests/test_operation_gate.py``).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from uwazi_admin_agent.domain.instance import instance_key


@dataclass(frozen=True)
class GateState:
    """The gate's current holder + its latest progress tick (immutable snapshot).

    Mutating functions replace this instance; readers for the UI work with
    ``snapshot()`` to get a complete and stable state.
    """

    instance: str
    kind: str
    run_id: str
    done: int
    total: int
    started_at: datetime
    note: str = ""
    """Operator-facing phase text ('verifying the selection…', 'bulk write
    refused — applying per entity'). Empty → the verb/fraction alone."""


@dataclass(frozen=True)
class RunClaim:
    """One local operation's exclusive claim on a run (immutable snapshot)."""

    instance: str
    run_id: str
    kind: str
    started_at: datetime


# The gated kinds, with the operator-facing words each one is shown as. Only
# operations that WRITE Uwazi take this gate.
KIND_VERBS: dict[str, str] = {
    "run": "Running",
    "revert": "Reverting",
}

# The unit whose count the progress fraction counts. A Run writes SUBOPERATIONS
# (the table's per-property rows: 1230 rows = 1230 units), a Revert restores
# ENTITIES (one backup per entity, whatever languages it wrote).
KIND_UNITS: dict[str, str] = {
    "run": "suboperations",
    "revert": "entity",
}

# Human-readable verbs for a per-run claim's refusal text.
RUN_CLAIM_VERBS: dict[str, str] = {
    "run": "running",
    "revert": "reverting",
    "creating": "generating",
    "refresh": "syncing with Uwazi",
    "renaming": "being renamed",
    "deleting": "being deleted",
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
    line = f"{verb} — {progress} {unit}…" if unit else f"{verb}…"
    return f"{line} {state.note.strip()}" if state.note else line


def busy_text(state: GateState | None) -> str:
    """The refusal toast's text when a second Uwazi write is attempted (pure)."""
    if state is None:
        return "Busy — another Uwazi operation is in progress. Wait until it finishes."
    name = f" on run {state.run_id!r}" if state.run_id else ""
    return f"Busy — {active_text(state).lower().capitalize()}{name}. Wait until it finishes."


def run_claim_text(claim: RunClaim | None) -> str:
    """The refusal text when the SAME run already has a local operation (pure)."""
    if claim is None:
        return "This task is busy with another operation. Wait until it finishes."
    verb = RUN_CLAIM_VERBS.get(claim.kind, claim.kind)
    return f"This task is busy — run {claim.run_id!r} is {verb}. Wait until it finishes."


_LOCK = threading.Lock()
_STATES: dict[str, GateState] = {}


def _key(instance: str) -> str:
    """The stable registry key of an instance URL, or '' for an unbound session."""
    return instance_key(instance) if (instance or "").strip() else ""


# --- Uwazi-write gate (one writer per instance) -------------------------------


def try_acquire(instance: str, kind: str, run_id: str) -> bool:
    """Claim the single Uwazi-write slot of ONE instance.

    False when another write already holds it, or when the session has no
    instance. The claim check and the write are ATOMIC under one lock: exactly
    one caller wins; every other gets False and reads its holder through
    :func:`snapshot`.
    """
    key = _key(instance)
    if not key:
        return False
    with _LOCK:
        if key in _STATES:
            return False
        _STATES[key] = GateState(
            instance=instance,
            kind=kind,
            run_id=run_id,
            done=0,
            total=0,
            started_at=datetime.now(timezone.utc),
        )
    return True


def release(instance: str, kind: str | None = None, run_id: str | None = None) -> None:
    """Drop ONE instance's claim when the caller still owns it.

    ``kind``/``run_id`` are the ownership check: a slow task releasing after its
    claim was already taken over must not free another op's slot.
    """
    key = _key(instance)
    if not key:
        return
    with _LOCK:
        state = _STATES.get(key)
        if state is None:
            return
        if kind is not None and state.kind != kind:
            return
        if run_id is not None and state.run_id != run_id:
            return
        _STATES.pop(key, None)


def snapshot(instance: str) -> GateState | None:
    """The instance's current holder + progress, or None when idle/unbound."""
    key = _key(instance)
    if not key:
        return None
    with _LOCK:
        return _STATES.get(key)


def set_progress(instance: str, done: int, total: int, note: str | None = None) -> None:
    """Record one progress tick for an instance (thread-safe; worker threads).

    ``total`` 0 means the unit count is still unknown (an indeterminate pass):
    the toast then shows '…' and an infinite bar. Ints carry the fraction; a
    holder-less write (a late report from a released op) is ignored.

    ``note`` names the PHASE the pass is in. ``None`` (the default from the
    two-argument reports) KEEPS the current note so a batch's fraction tick
    never wipes a phase message; ``""`` clears it (the phase is over); a
    non-empty string replaces it.
    """
    key = _key(instance)
    if not key:
        return
    with _LOCK:
        state = _STATES.get(key)
        if state is None:
            return
        current_note = state.note if note is None else note
        _STATES[key] = GateState(
            instance=state.instance,
            kind=state.kind,
            run_id=state.run_id,
            done=int(done),
            total=int(total),
            started_at=state.started_at,
            note=current_note,
        )


def set_note(instance: str, note: str) -> None:
    """Publish one operator-facing phase note for an instance (thread-safe)."""
    key = _key(instance)
    if not key:
        return
    with _LOCK:
        state = _STATES.get(key)
        if state is None:
            return
        _STATES[key] = GateState(
            instance=state.instance,
            kind=state.kind,
            run_id=state.run_id,
            done=state.done,
            total=state.total,
            started_at=state.started_at,
            note=note,
        )


# --- per-run claims (local work, scoped to one run) ---------------------------

_CLAIM_LOCK = threading.Lock()
_CLAIMS: dict[str, dict[str, RunClaim]] = {}


def try_claim_run(instance: str, run_id: str, kind: str) -> bool:
    """Claim exclusive local work on ONE run of an instance (False when taken).

    The claim is independent of the instance write gate: it only prevents two
    operations from mutating the SAME run's folder/table at once. Different runs
    of the same instance stay independent.
    """
    key = _key(instance)
    if not key or not run_id:
        return False
    with _CLAIM_LOCK:
        claims = _CLAIMS.setdefault(key, {})
        if run_id in claims:
            return False
        claims[run_id] = RunClaim(
            instance=instance,
            run_id=run_id,
            kind=kind,
            started_at=datetime.now(timezone.utc),
        )
    return True


def try_claim_runs(instance: str, run_ids: list[str], kind: str) -> bool:
    """Claim several runs ATOMICALLY (bulk delete/rename over a selection).

    Either every run is free and all are claimed, or nothing changes. Sorted
    acquisition makes concurrent bulk claims deterministic.
    """
    key = _key(instance)
    unique = sorted({run_id for run_id in run_ids if run_id})
    if not key or not unique:
        return False
    with _CLAIM_LOCK:
        claims = _CLAIMS.get(key) or {}
        if any(run_id in claims for run_id in unique):
            return False
        now = datetime.now(timezone.utc)
        bucket = _CLAIMS.setdefault(key, {})
        for run_id in unique:
            bucket[run_id] = RunClaim(instance=instance, run_id=run_id, kind=kind, started_at=now)
    return True


def release_run(instance: str, run_id: str, kind: str | None = None) -> None:
    """Release ONE run claim when the caller still owns it (idempotent)."""
    key = _key(instance)
    if not key:
        return
    with _CLAIM_LOCK:
        claims = _CLAIMS.get(key)
        if not claims:
            return
        claim = claims.get(run_id)
        if claim is None:
            return
        if kind is not None and claim.kind != kind:
            return
        claims.pop(run_id, None)
        if not claims:
            _CLAIMS.pop(key, None)


def release_runs(instance: str, run_ids: list[str], kind: str | None = None) -> None:
    """Release several run claims owned by the caller (idempotent)."""
    for run_id in dict.fromkeys(run_ids):
        release_run(instance, run_id, kind)


def run_claim(instance: str, run_id: str) -> RunClaim | None:
    """The claim on ONE run, or None when it is free/unbound."""
    key = _key(instance)
    if not key or not run_id:
        return None
    with _CLAIM_LOCK:
        return (_CLAIMS.get(key) or {}).get(run_id)


def run_claims(instance: str) -> tuple[RunClaim, ...]:
    """Every local operation currently claimed on an instance (a stable tuple)."""
    key = _key(instance)
    if not key:
        return ()
    with _CLAIM_LOCK:
        return tuple((_CLAIMS.get(key) or {}).values())


__all__ = [
    "GateState",
    "KIND_UNITS",
    "KIND_VERBS",
    "RUN_CLAIM_VERBS",
    "RunClaim",
    "active_text",
    "busy_text",
    "progress_text",
    "release",
    "release_run",
    "release_runs",
    "run_claim",
    "run_claim_text",
    "run_claims",
    "set_note",
    "set_progress",
    "snapshot",
    "try_acquire",
    "try_claim_run",
    "try_claim_runs",
]
