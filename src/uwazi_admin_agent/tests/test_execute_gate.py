"""Isolated unit tests for the execute gate (:mod:`uwazi_admin_agent.domain.execute_gate`).

Covers the re-execute accumulation fix: the gate refuses FAILED runs (operator
must revert first) and allows re-execute on all other statuses (including
EXECUTED), signaling ``needs_reset`` when a touch set is already present so
re-execution starts clean.
"""

from __future__ import annotations

import pytest

from uwazi_admin_agent.domain.execute_gate import (
    ExecuteRefusedError,
    decide_execute_gate,
)
from uwazi_admin_agent.domain.manifest import RunStatus

# --- refuse cases ------------------------------------------------------------


def test_gate_refuses_failed() -> None:
    decision = decide_execute_gate(RunStatus.FAILED, has_touch_set=True)
    assert decision.action == "refuse"
    assert decision.reason is not None
    assert "revert" in decision.reason


def test_gate_refuses_failed_even_without_touch_set() -> None:
    # Defensive: a FAILED manifest should never have an empty touch set, but
    # the gate refuses on status alone — it must not silently re-execute.
    decision = decide_execute_gate(RunStatus.FAILED, has_touch_set=False)
    assert decision.action == "refuse"


# --- allow cases -------------------------------------------------------------


def test_gate_allows_planned_without_touch_set_no_reset() -> None:
    decision = decide_execute_gate(RunStatus.PLANNED, has_touch_set=False)
    assert decision.action == "allow"
    assert decision.needs_reset is False


def test_gate_allows_reverted_and_signals_reset() -> None:
    decision = decide_execute_gate(RunStatus.REVERTED, has_touch_set=True)
    assert decision.action == "allow"
    assert decision.needs_reset is True


def test_gate_allows_reverted_without_touch_set_no_reset() -> None:
    decision = decide_execute_gate(RunStatus.REVERTED, has_touch_set=False)
    assert decision.action == "allow"
    assert decision.needs_reset is False


def test_gate_allows_verified_and_signals_reset_when_touch_set_present() -> None:
    decision = decide_execute_gate(RunStatus.VERIFIED, has_touch_set=True)
    assert decision.action == "allow"
    assert decision.needs_reset is True


def test_gate_allows_planned_but_resets_if_touch_set_present() -> None:
    # Defensive: a PLANNED manifest carrying stale touch-set entries is reset.
    decision = decide_execute_gate(RunStatus.PLANNED, has_touch_set=True)
    assert decision.action == "allow"
    assert decision.needs_reset is True


def test_gate_allows_executed_and_signals_reset() -> None:
    # EXECUTED is allowed (maintenance tasks can re-run without revert).
    # needs_reset clears the stale touch set so the intercept repopulates it.
    decision = decide_execute_gate(RunStatus.EXECUTED, has_touch_set=True)
    assert decision.action == "allow"
    assert decision.needs_reset is True


def test_gate_allows_executed_without_touch_set_no_reset() -> None:
    # Defensive: an EXECUTED manifest should never have an empty touch set,
    # but the gate allows it without reset if so.
    decision = decide_execute_gate(RunStatus.EXECUTED, has_touch_set=False)
    assert decision.action == "allow"
    assert decision.needs_reset is False


# --- decision is immutable ---------------------------------------------------


def test_gate_decision_is_frozen() -> None:
    decision = decide_execute_gate(RunStatus.PLANNED, has_touch_set=False)
    with pytest.raises(Exception):
        decision.action = "refuse"  # type: ignore[misc]


# --- exception type ----------------------------------------------------------


def test_execute_refused_error_carries_message() -> None:
    with pytest.raises(ExecuteRefusedError, match="revert"):
        raise ExecuteRefusedError("run previously failed; revert the partial first")
