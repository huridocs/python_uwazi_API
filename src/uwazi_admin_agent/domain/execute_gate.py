"""Pure gate deciding whether a run may be (re-)executed (Phase 6 safety follow-up).

Re-execute accumulation bug: ``execute`` loaded the persisted manifest (which
already carried the previous run's touch set) and appended to it on every call,
so ``modified`` grew by the touch set on each re-execute (2 -> 4 -> 6 ...) even
though the real touch set was unchanged. The fix is a state gate: refuse to
re-execute a run whose status is ``FAILED`` (the operator must revert first to
avoid re-running over a partially-applied state); for any other status
(including ``EXECUTED``), allow — and reset the touch set when one is already
present so re-execution starts clean (maintenance tasks can run repeatedly
without manual revert between runs).

This module holds the **pure** decision (no I/O) so it is the unit-test target.
The use case applies it: on ``refuse`` it raises :class:`ExecuteRefusedError`;
on ``allow`` with ``needs_reset`` it clears the manifest's touch-set lists and
the run's snapshots before running.
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
    (re-running over a partially-applied state is unsafe). All other statuses
    (including ``EXECUTED``) are allowed; ``needs_reset`` is True when a touch
    set is already present, so the caller clears it before running — this lets
    maintenance tasks re-execute repeatedly without manual revert.
    """
    if status == RunStatus.FAILED:
        return ExecuteGateDecision(action="refuse", reason="run previously failed; revert the partial first")
    if status == RunStatus.GENERATION_FAILED:
        return ExecuteGateDecision(action="refuse", reason="script generation failed; delete or retry the task")
    return ExecuteGateDecision(action="allow", needs_reset=has_touch_set)
