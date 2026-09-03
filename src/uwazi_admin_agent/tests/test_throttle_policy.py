"""Isolated unit tests for the pure auto-throttle policy (per AGENTS.md).

Pure: literal result dicts, literal states, plain bounds — no I/O, no mocks,
no network. Pins the operator's two-line contract: a Uwazi complaint descends
the worker allowance toward 1, and clean batches climb it back toward 4
incrementally.
"""

from uwazi_admin_agent.domain.batch_outcome import BatchOutcome, BatchVerdict
from uwazi_admin_agent.domain.throttle_policy import (
    classify_mutation_results,
    is_rate_limit_text,
    next_throttle_state,
    verdict_from_error_text,
)
from uwazi_admin_agent.domain.throttle_state import ThrottleState

_MIN = 1
_MAX = 4
_STREAK = 3


def _advance(state: ThrottleState, verdict: BatchVerdict) -> ThrottleState:
    return next_throttle_state(state, verdict, _MIN, _MAX, _STREAK)


# --- classify_mutation_results ------------------------------------------------


def test_clean_results_classify_as_clean() -> None:
    outcome = classify_mutation_results([{"shared_id": "A", "success": True}, {"shared_id": "B", "success": True}])
    assert outcome == BatchOutcome(success_count=2, failure_count=0, rate_limited_count=0, verdict=BatchVerdict.CLEAN)


def test_rate_limited_error_code_classifies_as_rate_limited() -> None:
    results = [{"shared_id": "A", "success": True}, {"shared_id": "B", "success": False, "error_code": "RATE_LIMITED"}]
    outcome = classify_mutation_results(results)
    assert outcome.verdict is BatchVerdict.RATE_LIMITED
    assert outcome.rate_limited_count == 1
    assert outcome.failure_count == 1


def test_rate_limit_error_text_classifies_as_rate_limited() -> None:
    results = [{"shared_id": "A", "success": False, "error": "API error (429)"}]
    assert classify_mutation_results(results).verdict is BatchVerdict.RATE_LIMITED


def test_plain_failure_classifies_as_degraded() -> None:
    results = [{"shared_id": "A", "success": False, "error": "not a valid thesaurus label", "error_code": "INVALID_LABEL"}]
    outcome = classify_mutation_results(results)
    assert outcome.verdict is BatchVerdict.DEGRADED
    assert outcome.rate_limited_count == 0


def test_relationship_shaped_results_classify_by_success_and_text() -> None:
    """The relationship port's results carry no error_code — text/success still classify."""
    results = [
        {"success": True, "from_entity_shared_id": "A", "to_entity_shared_id": "B"},
        {"success": False, "error": "API error (502)"},
    ]
    outcome = classify_mutation_results(results)
    assert outcome.verdict is BatchVerdict.DEGRADED
    assert outcome.success_count == 1


def test_classify_skips_non_dict_entries() -> None:
    outcome = classify_mutation_results(["Error: unknown mode 'x'", {"shared_id": "A", "success": True}])  # type: ignore[list-item]
    assert outcome.success_count == 1
    assert outcome.verdict is BatchVerdict.CLEAN


# --- is_rate_limit_text / verdict_from_error_text -----------------------------


def test_rate_limit_markers_match() -> None:
    for text in (
        "Max retries exceeded: too many 429 error responses",
        "API error (429)",
        "RATE_LIMITED",
        "Request failed: rate limit exceeded",
        "Too Many Requests",
    ):
        assert is_rate_limit_text(text), text


def test_words_containing_rate_do_not_false_positive() -> None:
    for text in ("generate", "accurate", "separate report", "moderate delay", "", None):
        assert not is_rate_limit_text(text), text


def test_verdict_from_error_text_splits_load_complaints_from_generic_errors() -> None:
    assert verdict_from_error_text("HTTP 429 too many requests") is BatchVerdict.RATE_LIMITED
    assert verdict_from_error_text("connection reset by peer") is BatchVerdict.DEGRADED
    assert verdict_from_error_text(None) is BatchVerdict.DEGRADED


# --- next_throttle_state: the descend-then-recover contract -------------------


def test_complaints_descend_the_allowance_toward_one() -> None:
    state = ThrottleState(workers=4)
    for expected in (3, 2, 1, 1):  # the floor holds — it never goes below min
        state = _advance(state, BatchVerdict.RATE_LIMITED)
        assert state.workers == expected
    assert state.complaint_count == 4
    assert state.success_streak == 0


def test_clean_streaks_climb_back_incrementally() -> None:
    state = ThrottleState(workers=1)
    state = _advance(state, BatchVerdict.CLEAN)
    state = _advance(state, BatchVerdict.CLEAN)
    assert state.workers == 1  # two clean batches are not enough yet
    state = _advance(state, BatchVerdict.CLEAN)
    assert state.workers == 2  # the promotion_streak-th clean batch promotes
    state = _advance(state, BatchVerdict.CLEAN)
    assert state.workers == 3  # the streak is KEPT across a promotion: recovery is fast
    state = _advance(state, BatchVerdict.CLEAN)
    assert state.workers == 4
    state = _advance(state, BatchVerdict.CLEAN)
    assert state.workers == 4  # the ceiling holds


def test_clean_batches_below_min_do_not_promote_early() -> None:
    """At the floor with an unmet streak, clean batches accumulate — never below-bounds."""
    state = _advance(ThrottleState(workers=1), BatchVerdict.CLEAN)
    assert state.workers == 1
    assert state.success_streak == 1


def test_degraded_resets_the_streak_without_touching_the_allowance() -> None:
    state = ThrottleState(workers=3, success_streak=2)
    state = _advance(state, BatchVerdict.DEGRADED)
    assert state.workers == 3
    assert state.success_streak == 0
    assert state.complaint_count == 0


def test_complaint_then_clean_streak_recovers_the_lost_step() -> None:
    state = ThrottleState(workers=4)
    state = _advance(state, BatchVerdict.RATE_LIMITED)
    assert state.workers == 3
    for _ in range(3):
        state = _advance(state, BatchVerdict.CLEAN)
    assert state.workers == 4
