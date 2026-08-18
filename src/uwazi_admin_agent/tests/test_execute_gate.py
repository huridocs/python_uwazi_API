"""Isolated unit tests for the execute gate (:mod:`uwazi_admin_agent.domain.execute_gate`).

Covers the re-execute accumulation fix: the gate refuses EXECUTED/FAILED runs
(operator must revert first) and allows re-execute on other statuses, signaling
``needs_reset`` when a touch set is already present.
"""

from __future__ import annotations

import pytest

from uwazi_admin_agent.domain.execute_gate import (
    ExecuteRefusedError,
    decide_execute_gate,
)
from uwazi_admin_agent.domain.manifest import RunStatus

# --- refuse cases ------------------------------------------------------------


def test_gate_refuses_executed() -> None:
    decision = decide_execute_gate(RunStatus.EXECUTED, has_touch_set=True)
    assert decision.action == "refuse"
    assert decision.reason is not None
    assert "revert" in decision.reason


def test_gate_refuses_failed() -> None:
    decision = decide_execute_gate(RunStatus.FAILED, has_touch_set=True)
    assert decision.action == "refuse"
    assert decision.reason is not None
    assert "revert" in decision.reason


def test_gate_refuses_executed_even_without_touch_set() -> None:
    # Defensive: an EXECUTED manifest should never have an empty touch set, but
    # the gate refuses on status alone — it must not silently re-execute.
    decision = decide_execute_gate(RunStatus.EXECUTED, has_touch_set=False)
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


# --- decision is immutable ---------------------------------------------------


def test_gate_decision_is_frozen() -> None:
    decision = decide_execute_gate(RunStatus.PLANNED, has_touch_set=False)
    with pytest.raises(Exception):
        decision.action = "refuse"  # type: ignore[misc]


# --- exception type ----------------------------------------------------------


def test_execute_refused_error_carries_message() -> None:
    with pytest.raises(ExecuteRefusedError, match="revert"):
        raise ExecuteRefusedError("run already executed; revert first to re-run")
