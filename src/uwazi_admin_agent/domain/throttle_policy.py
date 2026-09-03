"""Pure auto-throttle policy: classify a batch, then advance the state.

One :class:`ThrottleState` lives for a whole execute (or dry-run) pass and is
advanced by this policy after every parallel helper call:

- ``RATE_LIMITED`` (Uwazi complained about load): the worker allowance drops
  by one — 4 -> 3 -> 2 -> 1, never below ``min_workers`` — and the success
  streak resets. Complaining batches never promote.
- ``DEGRADED`` (per-entity failures with no load signal): the streak resets
  but the allowance is untouched — a bad thesaurus label is not Uwazi
  struggling with concurrency, and throttling would not fix it.
- ``CLEAN``: the streak grows; once it reaches ``promotion_streak`` the
  allowance climbs by one, up to ``max_workers``. The streak is KEPT across a
  promotion (only complaints/degradation reset it), so the climb back to the
  cap is as fast as the descent — 3 clean batches per step.

All bounds are arguments, not imports: the domain stays configuration-free
(the caller — :class:`ThrottleController` — applies the constants, mirroring
how ``enforce_cap`` takes the cap). Pure: no I/O, no clocks, no globals.
"""

from __future__ import annotations

from typing import Any

from uwazi_admin_agent.domain.batch_outcome import BatchOutcome, BatchVerdict
from uwazi_admin_agent.domain.throttle_state import ThrottleState

# Error-text markers of a load complaint, lowercased before matching. Mirrors
# uwazi_agent's _categorize_publish_error markers, tightened so plain words
# containing "rate" (generate, accurate, separate) cannot false-positive.
_RATE_LIMIT_MARKERS: tuple[str, ...] = (
    "rate_limited",
    "rate limit",
    "ratelimit",
    "rate-limit",
    "429",
    "too many requests",
    "too many 429",
)


def is_rate_limit_text(text: str | None) -> bool:
    """True when an error text is Uwazi (or a fronting limiter) complaining about load."""
    if not text:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in _RATE_LIMIT_MARKERS)


def verdict_from_error_text(text: str | None) -> BatchVerdict:
    """Classify a raised error: a load complaint, or a generic degraded batch."""
    return BatchVerdict.RATE_LIMITED if is_rate_limit_text(text) else BatchVerdict.DEGRADED


def classify_mutation_results(results: list[Any]) -> BatchOutcome:
    """Classify per-entity mutation result dicts into batch evidence.

    Accepts the dumped result dicts both the entity CRUD ports and the
    relationship port return (``success``/``error``/optional ``error_code``):
    a failure counts as a load complaint iff its ``error_code`` is
    ``RATE_LIMITED`` or its error text carries a load marker. Typed ``Any``
    item-wise (not ``dict``): a script-facing helper can receive non-dicts,
    and the isinstance guard below must stay REACHABLE, not "proven" dead
    by the type checker.
    """
    success = 0
    failures = 0
    rate_limited = 0
    for r in results:
        if not isinstance(r, dict):
            continue
        if r.get("success"):
            success += 1
            continue
        failures += 1
        if r.get("error_code") == "RATE_LIMITED" or is_rate_limit_text(r.get("error")):
            rate_limited += 1
    if rate_limited:
        verdict = BatchVerdict.RATE_LIMITED
    elif failures:
        verdict = BatchVerdict.DEGRADED
    else:
        verdict = BatchVerdict.CLEAN
    return BatchOutcome(success_count=success, failure_count=failures, rate_limited_count=rate_limited, verdict=verdict)


def next_throttle_state(
    state: ThrottleState,
    verdict: BatchVerdict,
    min_workers: int,
    max_workers: int,
    promotion_streak: int,
) -> ThrottleState:
    """Pure: the next :class:`ThrottleState` after one batch verdict.

    ``promotion_streak`` clean batches in a row promote the allowance by one
    (up to ``max_workers``); the streak survives a promotion so recovery is
    continuous. ``RATE_LIMITED`` demotes by one (down to ``min_workers``) and
    resets the streak; ``DEGRADED`` only resets the streak.
    """
    if verdict is BatchVerdict.RATE_LIMITED:
        return state.model_copy(
            update={
                "workers": max(min_workers, state.workers - 1),
                "success_streak": 0,
                "complaint_count": state.complaint_count + 1,
            }
        )
    if verdict is BatchVerdict.DEGRADED:
        return state.model_copy(update={"success_streak": 0})
    streak = state.success_streak + 1
    if streak >= promotion_streak and state.workers < max_workers:
        return state.model_copy(update={"workers": state.workers + 1, "success_streak": streak})
    return state.model_copy(update={"success_streak": streak})
