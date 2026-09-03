"""The in-process auto-throttle controller: one worker allowance per run pass.

One controller lives for a whole execute pass (or one dry-run pass) and is
shared by every ``*_parallel`` script helper: helpers read their current
allowance here before fanning a batch out, and report the batch's
:class:`BatchVerdict` here afterwards — so a complaint in a write batch backs
off the READS too, and clean read streaks help the writes climb back. The
state transitions are the pure policy (:mod:`...domain.throttle_policy`);
this class adds the configuration constants, the transition log line, and
the brief pause after a load complaint.

Single-threaded by construction, so it carries no lock: every
:meth:`record` happens in the script's worker thread (the helpers are sync
and the executor's pool is joined before any verdict is classified).
Constants are applied here rather than in the domain, mirroring how
:class:`BackupIntercept` applies ``MAX_ENTITIES_PER_RUN`` via ``enforce_cap``.
"""

from __future__ import annotations

import time

from loguru import logger

from uwazi_admin_agent.configuration import (
    THROTTLE_MAX_WORKERS,
    THROTTLE_MIN_WORKERS,
    THROTTLE_PENALTY_PAUSE_SECONDS,
    THROTTLE_PROMOTION_STREAK,
)
from uwazi_admin_agent.domain.batch_outcome import BatchVerdict
from uwazi_admin_agent.domain.throttle_policy import next_throttle_state
from uwazi_admin_agent.domain.throttle_state import ThrottleState


class ThrottleController:
    """Advance one :class:`ThrottleState` per reported batch verdict."""

    def __init__(
        self,
        min_workers: int = THROTTLE_MIN_WORKERS,
        max_workers: int = THROTTLE_MAX_WORKERS,
        promotion_streak: int = THROTTLE_PROMOTION_STREAK,
        pause_seconds: float = THROTTLE_PENALTY_PAUSE_SECONDS,
    ) -> None:
        self._min_workers: int = min_workers
        self._max_workers: int = max_workers
        self._promotion_streak: int = promotion_streak
        self._pause_seconds: float = pause_seconds
        self._state: ThrottleState = ThrottleState(workers=max_workers)

    def allowance(self) -> int:
        """How many parallel workers may run for the NEXT batch (requests in flight)."""
        return self._state.workers

    def record(self, verdict: BatchVerdict) -> ThrottleState:
        """Apply the policy to one reported verdict; pause briefly on a load complaint."""
        before = self._state.workers
        self._state = next_throttle_state(self._state, verdict, self._min_workers, self._max_workers, self._promotion_streak)
        self._log_transition(before, verdict)
        if verdict is BatchVerdict.RATE_LIMITED and self._pause_seconds > 0:
            time.sleep(self._pause_seconds)
        return self.snapshot()

    def snapshot(self) -> ThrottleState:
        """A copy of the current state (for log lines / run reports)."""
        return self._state.model_copy()

    def _log_transition(self, before: int, verdict: BatchVerdict) -> None:
        workers = self._state.workers
        if workers != before:
            logger.info("throttle: workers {} -> {} after {} batch", before, workers, verdict.value)
        else:
            logger.debug("throttle: workers {} steady after {} batch", workers, verdict.value)
