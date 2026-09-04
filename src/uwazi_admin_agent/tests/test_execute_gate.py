"""Isolated unit tests for the execute gate (:mod:`uwazi_admin_agent.domain.execute_gate).

Covers the consecutive-execute fix: the gate refuses a run in an
APPLIED-but-not-reverted state with a live touch set (``EXECUTED`` — the
operator's live report of the data-loss hole; ``VERIFIED`` — the same class,
an executed run whose verification passed), and also refuses ``FAILED`` /
``GENERATION_FAILED``. Everything else is allowed — ``REVERTED`` is the
confirmed working cycle (dedupe -> revert -> dedupe again) and signals
``needs_reset`` so re-execution starts clean; ``PLANNED``/``SNAPSHOTTED`` are
pre-execution (a touch set there is stale/defensive and gets reset);
``EXECUTED``/``VERIFIED`` with an EMPTY touch set is allowed (defensive:
nothing to lose).
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


def test_generation_failed_refused() -> None:
    decision = decide_execute_gate(RunStatus.GENERATION_FAILED, has_touch_set=False)
    assert decision.action == "refuse"
    assert decision.reason is not None
    assert "generation" in decision.reason


def test_gate_refuses_executed_with_touch_set() -> None:
    """THE hole: an EXECUTED-not-reverted run must never re-execute — the
    reset would wipe the manifest records + backup bytes that are its only
    path back while its deletes stay live (unrevertable)."""
    decision = decide_execute_gate(RunStatus.EXECUTED, has_touch_set=True)
    assert decision.action == "refuse"
    assert decision.needs_reset is False  # a refusal must NEVER signal a reset
    assert decision.reason is not None
    assert "revert" in decision.reason
    assert "executed" in decision.reason
    assert "new task" in decision.reason


def test_gate_refuses_verified_with_touch_set() -> None:
    """VERIFIED is applied-not-reverted, the same class as EXECUTED: its
    touch set + backups are live, so re-executing would make it unrevertable."""
    decision = decide_execute_gate(RunStatus.VERIFIED, has_touch_set=True)
    assert decision.action == "refuse"
    assert decision.needs_reset is False
    assert decision.reason is not None
    assert "revert" in decision.reason


def test_gate_allows_executed_without_touch_set_no_reset() -> None:
    # Defensive: an EXECUTED manifest should never have an empty touch set,
    # but if a no-op script ran there is nothing to lose — allow without reset.
    decision = decide_execute_gate(RunStatus.EXECUTED, has_touch_set=False)
    assert decision.action == "allow"
    assert decision.needs_reset is False


def test_gate_allows_verified_without_touch_set_no_reset() -> None:
    # Same defensive row as EXECUTED-without-touch-set: nothing to protect.
    decision = decide_execute_gate(RunStatus.VERIFIED, has_touch_set=False)
    assert decision.action == "allow"
    assert decision.needs_reset is False


# --- allow cases -------------------------------------------------------------


def test_gate_allows_planned_without_touch_set_no_reset() -> None:
    decision = decide_execute_gate(RunStatus.PLANNED, has_touch_set=False)
    assert decision.action == "allow"
    assert decision.needs_reset is False


def test_gate_allows_reverted_and_signals_reset() -> None:
    """THE confirmed working cycle (dedupe -> revert -> dedupe again): a
    REVERTED run re-executes with a clean touch set."""
    decision = decide_execute_gate(RunStatus.REVERTED, has_touch_set=True)
    assert decision.action == "allow"
    assert decision.needs_reset is True


def test_gate_allows_reverted_without_touch_set_no_reset() -> None:
    decision = decide_execute_gate(RunStatus.REVERTED, has_touch_set=False)
    assert decision.action == "allow"
    assert decision.needs_reset is False


def test_gate_allows_planned_but_resets_if_touch_set_present() -> None:
    # Defensive: a PLANNED manifest carrying stale touch-set entries is reset.
    decision = decide_execute_gate(RunStatus.PLANNED, has_touch_set=True)
    assert decision.action == "allow"
    assert decision.needs_reset is True


def test_gate_treats_snapshotted_as_pre_execution() -> None:
    """Nothing sets SNAPSHOTTED today; this codebase snapshots inside
    execute, so it is a pre-execution state (like PLANNED): a touch set
    there is stale and gets reset, an empty one needs no reset."""
    with_touch_set = decide_execute_gate(RunStatus.SNAPSHOTTED, has_touch_set=True)
    assert with_touch_set.action == "allow"
    assert with_touch_set.needs_reset is True
    without_touch_set = decide_execute_gate(RunStatus.SNAPSHOTTED, has_touch_set=False)
    assert without_touch_set.action == "allow"
    assert without_touch_set.needs_reset is False


# --- decision is immutable ---------------------------------------------------


def test_gate_decision_is_frozen() -> None:
    decision = decide_execute_gate(RunStatus.PLANNED, has_touch_set=False)
    with pytest.raises(Exception):
        decision.action = "refuse"


# --- exception type ----------------------------------------------------------


def test_execute_refused_error_carries_message() -> None:
    with pytest.raises(ExecuteRefusedError, match="revert"):
        raise ExecuteRefusedError("run previously failed; revert the partial first")
