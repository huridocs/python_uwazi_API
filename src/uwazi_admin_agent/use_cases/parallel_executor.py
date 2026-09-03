"""Bounded thread-pool runner for the parallel script helpers.

Runs zero-arg sync tasks with at most ``controller.allowance()`` worker
threads. Tasks wrap ONE port call each in ``asyncio.run``:

- the write ports' coroutines offload every HTTP call via ``asyncio.to_thread``
  themselves, so they run correctly under a fresh loop per task;
- the raw entity/file repositories are async BY SIGNATURE but block inside
  the coroutine (plain ``requests``), so they MUST run on their own thread to
  overlap at all — gathering them on the script's loop would serialize.

A task that raises stores its exception at its index; :meth:`run` never
raises a task's exception itself — the caller decides (writes re-raise the
first error, reads retry once serially). The pool is created per call so the
worker count tracks the live throttle allowance between batches; it is fully
joined before ``run`` returns, so everything after it (verdict recording,
cache invalidation, audit, manifest writes) stays on the script's thread.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from uwazi_admin_agent.use_cases.throttle_controller import ThrottleController


class ParallelExecutor:
    """Run zero-arg tasks in a thread pool bounded by the throttle allowance."""

    def __init__(self, controller: ThrottleController) -> None:
        self._controller: ThrottleController = controller

    def allowance(self) -> int:
        """The current worker allowance (pool size for the next :meth:`run`)."""
        return self._controller.allowance()

    def record(self, verdict: Any) -> None:
        """Report a batch verdict to the throttle (transition log + pause live there)."""
        self._controller.record(verdict)

    def run(self, tasks: list[Callable[[], Any]]) -> tuple[list[Any | None], list[BaseException | None]]:
        """Execute ``tasks`` concurrently; return position-aligned ``(values, errors)``.

        A failed task contributes ``None`` at its index in ``values`` and the
        exception at its index in ``errors``; a successful one its return value
        and ``None``. Empty input short-circuits without touching the pool.
        """
        if not tasks:
            return [], []
        values: list[Any | None] = [None] * len(tasks)
        errors: list[BaseException | None] = [None] * len(tasks)
        with ThreadPoolExecutor(max_workers=self.allowance()) as pool:
            futures = {pool.submit(task): index for index, task in enumerate(tasks)}
            for future, index in futures.items():
                try:
                    values[index] = future.result()
                except Exception as exc:  # noqa: BLE001 — captured positionally; the caller decides
                    errors[index] = exc
        return values, errors
