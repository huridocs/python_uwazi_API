"""Isolated unit tests for the ParallelExecutor (per AGENTS.md).

Real controller + real executor + real zero-arg callables over literals —
no ports, no mocks, no network. ``threading.Barrier`` proves the actual
contract with the pool's own semantics: the allowance is how many tasks may
run at once (a barrier sized allowance+1 must time out; one sized allowance
must trip), and the pool size follows the controller between calls.
"""

import threading

from uwazi_admin_agent.domain.batch_outcome import BatchVerdict
from uwazi_admin_agent.use_cases.parallel_executor import ParallelExecutor
from uwazi_admin_agent.use_cases.throttle_controller import ThrottleController

# Long enough for a passing pair to meet, short enough that a bound-violation
# wait costs ~2s; barriers only time out on the FAILURE path.
_BARRIER_TIMEOUT: float = 2.0


def _executor(max_workers: int = 4) -> ParallelExecutor:
    controller = ThrottleController(min_workers=1, max_workers=max_workers, promotion_streak=3, pause_seconds=0.0)
    return ParallelExecutor(controller)


def test_empty_input_short_circuits() -> None:
    assert _executor().run([]) == ([], [])


def test_results_are_position_aligned() -> None:
    values, errors = _executor().run([lambda: "a", lambda: "b", lambda: "c"])
    assert values == ["a", "b", "c"]
    assert errors == [None, None, None]


def test_task_exceptions_are_captured_positionally_not_raised() -> None:
    boom = ValueError("chunk exploded")

    def failing() -> str:
        raise boom

    values, errors = _executor().run([lambda: "ok", failing, lambda: "also-ok"])
    assert values == ["ok", None, "also-ok"]
    assert errors[0] is None
    assert errors[1] is boom
    assert errors[2] is None


def test_run_holds_the_allowance_concurrency_bound() -> None:
    """A barrier sized allowance+1 must BREAK: the pool may never run that many at once."""
    executor = _executor(max_workers=2)
    too_many = threading.Barrier(3, timeout=_BARRIER_TIMEOUT)

    def task() -> str:
        try:
            too_many.wait()
            return "unbounded"  # 3 tasks ran concurrently — bound violated
        except threading.BrokenBarrierError:
            return "bounded"

    values, errors = executor.run([task] * 6)
    assert values == ["bounded"] * 6
    assert all(e is None for e in errors)


def test_run_actually_overlaps_upto_the_allowance() -> None:
    """A barrier sized exactly `allowance` must TRIP: the workers really run concurrently."""
    executor = _executor(max_workers=2)
    pair = threading.Barrier(2, timeout=_BARRIER_TIMEOUT)

    def task() -> str:
        pair.wait()
        return "paired"

    values, errors = executor.run([task] * 6)
    assert values == ["paired"] * 6
    assert all(e is None for e in errors)


def test_pool_size_follows_the_controller_between_calls() -> None:
    """After a complaint the allowance drops, and the NEXT run's pool is smaller."""
    executor = _executor(max_workers=2)
    executor.record(BatchVerdict.RATE_LIMITED)  # allowance 2 -> 1
    pair = threading.Barrier(2, timeout=_BARRIER_TIMEOUT)

    def task() -> str:
        try:
            pair.wait()
            return "paired"  # would mean 2 tasks ran concurrently
        except threading.BrokenBarrierError:
            return "serialized"

    values, _ = executor.run([task] * 4)
    assert values == ["serialized"] * 4
