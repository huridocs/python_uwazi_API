"""Pure gate deciding whether a run may be reverted (delete-revert safety follow-up).

Reverting a run that is already ``REVERTED`` would re-run the revert actions a
second time. For a run whose script **deleted** entities, that means
``RecreateEntityAction`` fires again and Uwazi mints *another* fresh ``sharedId``
per deleted entity — idempotency is broken and orphan entities leak into the
instance. The fix is a state gate: refuse to revert a run whose status is
``REVERTED`` (the operator must re-``execute`` first if they want to revert
again); any other status is allowed.

Mirrors :mod:`domain.execute_gate`: this module holds the **pure** decision
(no I/O) and is the unit-test target; the use case applies it (raising
:class:`RevertRefusedError` on refuse) and the driver surfaces the message.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from uwazi_admin_agent.domain.manifest import RunStatus


class RevertRefusedError(Exception):
    """Raised by the revert use case when the gate refuses a revert."""


class RevertGateDecision(BaseModel):
    """Outcome of :func:`decide_revert_gate`.

    ``action="refuse"`` carries a ``reason`` for the operator; ``action="allow"``
    means the run is not already reverted and may be reverted now.
    """

    model_config = ConfigDict(frozen=True)

    action: Literal["allow", "refuse"]
    reason: str | None = None


def decide_revert_gate(status: RunStatus) -> RevertGateDecision:
    """Pure: decide whether a run may be reverted given its current status.

    ``REVERTED`` is refused — re-reverting a delete-run would re-create its
    deleted entities a second time (new sharedIds each time), leaking orphans and
    breaking idempotency; the operator must re-``execute`` first. Any other
    status is allowed (``EXECUTED`` is the normal revert target; ``FAILED`` may
    be a partial run an operator wants to revert; an empty touch set reverts
    harmlessly).
    """
    if status == RunStatus.REVERTED:
        return RevertGateDecision(action="refuse", reason="run already reverted; re-execute first to revert again")
    return RevertGateDecision(action="allow")
