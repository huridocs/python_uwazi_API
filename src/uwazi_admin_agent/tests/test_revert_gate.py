"""Isolated unit tests for :func:`uwazi_admin_agent.domain.revert_gate.decide_revert_gate`.

Pure decision: literal statuses in, plain assertions out. No I/O, no mocks.
"""

import pytest

from uwazi_admin_agent.domain.manifest import RunStatus
from uwazi_admin_agent.domain.revert_gate import RevertRefusedError, decide_revert_gate


@pytest.mark.parametrize(
    "status",
    [RunStatus.PLANNED, RunStatus.SNAPSHOTTED, RunStatus.EXECUTED, RunStatus.VERIFIED, RunStatus.FAILED],
)
def test_allows_revert_for_non_reverted_statuses(status: RunStatus) -> None:
    decision = decide_revert_gate(status)
    assert decision.action == "allow"
    assert decision.reason is None


def test_refuses_revert_for_already_reverted() -> None:
    decision = decide_revert_gate(RunStatus.REVERTED)
    assert decision.action == "refuse"
    assert decision.reason is not None
    assert "already reverted" in decision.reason


def test_decision_is_immutable() -> None:
    decision = decide_revert_gate(RunStatus.EXECUTED)
    with pytest.raises(Exception):
        decision.action = "refuse"  # type: ignore[misc]


def test_refused_error_is_raisable() -> None:
    decision = decide_revert_gate(RunStatus.REVERTED)
    with pytest.raises(RevertRefusedError):
        raise RevertRefusedError(decision.reason or "")
