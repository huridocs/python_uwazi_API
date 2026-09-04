"""Pure gate deciding whether a run may be (re-)executed (Phase 6 safety follow-up).

Two re-execute hazards, one gate:

1. Re-execute accumulation: ``execute`` loads the persisted manifest (which
   already carries the previous run's touch set) and appended to it on every
   call, so ``modified`` grew by the touch set on each re-execute (2 -> 4 ->
   6 ...) even though the real touch set was unchanged. Fixed by resetting the
   touch set (``needs_reset``) before a legitimate re-execute.
2. Consecutive-execute data loss (the operator's live report: "we can run the
   same code without reverting... the script just was not able to properly
   dedup again"): an ``EXECUTED`` run's changes are still LIVE in Uwazi, and
   its ONLY path back is (manifest records + backed-up bytes) -> revert
   re-uploads. Re-executing without reverting first wiped BOTH — the reset
   cleared the manifest records and ``clear_run`` cleared the backup bytes —
   then re-ran the script against the already-deleted state (dedupe finds
   nothing; explicit deletes hit ``not_found``). The run ended ``EXECUTED``
   with an EMPTY touch set while run 1's deletes stayed live: UNREVERTABLE,
   the operator's #1 constraint violated. So a run in an APPLIED-but-not-
   reverted state (``EXECUTED``; ``VERIFIED`` — an executed run whose
   post-revert verification passed, nothing more) with a live touch set is
   now REFUSED; the operator must revert first (or create a new task).

This module holds the **pure** decision (no I/O) so it is the unit-test target.
The use case applies it: on ``refuse`` it raises :class:`ExecuteRefusedError`
BEFORE resetting or clearing anything — a refused execute leaves the run
exactly as revertable as before the attempt; on ``allow`` with ``needs_reset``
it clears the manifest's touch-set lists and the run's backups before running
(the ``REVERTED`` -> re-execute cycle relies on this: revert restores, then a
fresh execute pass starts clean).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from uwazi_admin_agent.domain.manifest import RunStatus


class ExecuteRefusedError(Exception):
    """Raised by the execute use case when the gate refuses a (re-)execute."""


class ScriptExecutionError(Exception):
    """Raised when a validated script fails mid-run (carries the full error detail)."""


class ExecuteGateDecision(BaseModel):
    """Outcome of :func:`decide_execute_gate`.

    ``action="refuse"`` carries a ``reason`` for the operator; ``action="allow"``
    sets ``needs_reset`` when an existing touch set must be cleared first.
    """

    model_config = ConfigDict(frozen=True)

    action: Literal["allow", "refuse"]
    needs_reset: bool = False
    reason: str | None = None


def decide_execute_gate(status: RunStatus, has_touch_set: bool) -> ExecuteGateDecision:
    """Pure: decide whether a run may be (re-)executed given its current state.

    ``FAILED`` is refused — a partial run must be reverted before re-executing
    (re-running over a partially-applied state is unsafe). ``EXECUTED`` and
    ``VERIFIED`` are refused when a touch set is live — the run is applied but
    not reverted, and re-executing would wipe the manifest records + backup
    bytes that are its only path back (the operator must revert first, or
    create a new task); with an EMPTY touch set they are allowed (defensive:
    nothing to lose, e.g. a no-op script ran). ``GENERATION_FAILED`` is
    refused (no script to run). Everything else — ``PLANNED``, ``SNAPSHOTTED``
    (pre-execution: snapshots happen inside execute, so a touch set there is
    stale), and ``REVERTED`` (the working cycle: revert -> re-execute) — is
    allowed, with ``needs_reset`` True when a touch set is already present so
    the caller clears it before running.
    """
    if status == RunStatus.FAILED:
        return ExecuteGateDecision(action="refuse", reason="run previously failed; revert the partial first")
    if status == RunStatus.GENERATION_FAILED:
        return ExecuteGateDecision(action="refuse", reason="script generation failed; delete or retry the task")
    if status in (RunStatus.EXECUTED, RunStatus.VERIFIED) and has_touch_set:
        return ExecuteGateDecision(
            action="refuse",
            reason="run already executed; revert it first (or create a new task) to run again",
        )
    return ExecuteGateDecision(action="allow", needs_reset=has_touch_set)
