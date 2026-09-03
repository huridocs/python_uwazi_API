"""The auto-throttle's state value: the parallel worker allowance + recovery bookkeeping.

A :class:`ThrottleState` is advanced by the pure policy in
:mod:`uwazi_admin_agent.domain.throttle_policy` after every parallel batch a
generated script runs: the current ``workers`` allowance (how many requests
may be in flight at once), the streak of consecutive clean batches, and how
many load complaints Uwazi has raised so far. This model only carries the
values; the transitions (and their bounds) are the policy module's job, so
both stay unit-testable with literals.

Pydantic so a state snapshot round-trips (log lines, run reports) for free,
and ``model_copy(update=...)`` keeps every transition allocation-light.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ThrottleState(BaseModel):
    """How many parallel workers may run, plus the streak/complaint bookkeeping."""

    workers: int = Field(description="Current parallel worker allowance (requests in flight at once).")
    success_streak: int = Field(
        default=0,
        description="Consecutive clean batches since the last complaint (drives promotion).",
    )
    complaint_count: int = Field(
        default=0,
        description="How many rate-limit complaints Uwazi has raised over the whole pass.",
    )
