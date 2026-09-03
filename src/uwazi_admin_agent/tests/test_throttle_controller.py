"""Isolated unit tests for the ThrottleController state machine (per AGENTS.md).

Real controller + real state objects, zero I/O, ``pause_seconds=0`` so no test
sleeps. Pins the operator-visible behavior: starts at the 4-worker max,
complaints walk it down to the 1-worker floor, clean batches walk it back.
"""

from uwazi_admin_agent.domain.batch_outcome import BatchVerdict
from uwazi_admin_agent.use_cases.throttle_controller import ThrottleController


def _controller(max_workers: int = 4) -> ThrottleController:
    return ThrottleController(min_workers=1, max_workers=max_workers, promotion_streak=3, pause_seconds=0.0)


def test_starts_at_the_worker_maximum() -> None:
    assert _controller().allowance() == 4
    assert _controller(max_workers=2).allowance() == 2


def test_complaints_descend_to_the_floor_then_hold() -> None:
    controller = _controller()
    for expected in (3, 2, 1, 1):
        state = controller.record(BatchVerdict.RATE_LIMITED)
        assert controller.allowance() == expected
        assert state.workers == expected
    assert state.complaint_count == 4


def test_clean_streaks_climb_back_to_the_maximum() -> None:
    controller = _controller()
    for _ in range(3):
        controller.record(BatchVerdict.RATE_LIMITED)
    for _ in range(3):
        controller.record(BatchVerdict.CLEAN)
    assert controller.allowance() == 2  # one recovered step after the descent
    for _ in range(3):
        controller.record(BatchVerdict.CLEAN)
    for _ in range(3):
        controller.record(BatchVerdict.CLEAN)
    assert controller.allowance() == 4  # fully recovered


def test_degraded_resets_the_streak_but_keeps_the_allowance() -> None:
    controller = _controller()
    controller.record(BatchVerdict.RATE_LIMITED)
    controller.record(BatchVerdict.RATE_LIMITED)  # allowance 2
    controller.record(BatchVerdict.CLEAN)
    controller.record(BatchVerdict.CLEAN)  # streak 2 — one clean short of promotion
    controller.record(BatchVerdict.DEGRADED)  # resets the streak, allowance untouched
    assert controller.allowance() == 2
    controller.record(BatchVerdict.CLEAN)
    controller.record(BatchVerdict.CLEAN)
    assert controller.allowance() == 2  # still only 2 clean since the reset
    controller.record(BatchVerdict.CLEAN)
    assert controller.allowance() == 3  # the reset streak counted from zero


def test_snapshot_is_a_copy_not_the_live_state() -> None:
    controller = _controller()
    snapshot = controller.snapshot()
    controller.record(BatchVerdict.RATE_LIMITED)
    assert snapshot.workers == 4  # the earlier snapshot still shows the old allowance
    assert controller.snapshot().workers == 3
