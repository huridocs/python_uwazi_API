"""Isolated tests for the tasks page's per-run busy state (per AGENTS.md).

Per AGENTS.md: pure, offline, no mocks. The state lives in the per-instance
gate/claim registries; each test scopes itself to its own instance URL and
releases what it took, so tests never depend on execution order.
"""

from __future__ import annotations

import pytest

from uwazi_admin_agent.drivers.web import app as tasks_page
from uwazi_admin_agent.drivers.web import operation_gate

INSTANCE = "http://ui-tasks.test"
OTHER = "http://ui-other.test"


@pytest.fixture(autouse=True)
def _reset_state():
    """Release everything these tests may have taken, before and after."""

    def _clear() -> None:
        operation_gate.release(INSTANCE)
        operation_gate.release(OTHER)
        for claim in operation_gate.run_claims(INSTANCE) + operation_gate.run_claims(OTHER):
            operation_gate.release_run(claim.instance, claim.run_id)

    _clear()
    yield
    _clear()


def test_an_idle_run_is_not_busy() -> None:
    assert tasks_page._run_in_flight(INSTANCE, "run-1") is None
    assert tasks_page._run_is_busy(INSTANCE, "run-1") is False


def test_a_local_claim_marks_only_its_own_run() -> None:
    assert operation_gate.try_claim_run(INSTANCE, "run-1", "creating") is True
    assert tasks_page._run_in_flight(INSTANCE, "run-1") == "creating"
    assert tasks_page._run_is_busy(INSTANCE, "run-1") is True
    # A different run of the same instance stays free.
    assert tasks_page._run_in_flight(INSTANCE, "run-2") is None
    assert tasks_page._run_is_busy(INSTANCE, "run-2") is False
    # Another instance's table never sees this claim.
    assert tasks_page._run_is_busy(OTHER, "run-1") is False


def test_the_write_gate_marks_its_run_running_or_reverting() -> None:
    try:
        assert operation_gate.try_acquire(INSTANCE, "run", "run-1") is True
        assert tasks_page._run_in_flight(INSTANCE, "run-1") == "running"
        assert tasks_page._run_in_flight(INSTANCE, "run-2") is None
    finally:
        operation_gate.release(INSTANCE, "run", "run-1")
    try:
        assert operation_gate.try_acquire(INSTANCE, "revert", "run-1") is True
        assert tasks_page._run_in_flight(INSTANCE, "run-1") == "reverting"
    finally:
        operation_gate.release(INSTANCE, "revert", "run-1")


def test_busy_text_names_the_run_and_what_it_is_doing() -> None:
    assert operation_gate.try_claim_run(INSTANCE, "run-1", "creating") is True
    text = tasks_page._busy_run_text(INSTANCE, "run-1")
    assert "run-1" in text
    assert "generating" in text
    # A free run never produces a refusal.
    assert not tasks_page._busy_run_text(INSTANCE, "run-2").startswith("Busy")


def test_bulk_claim_is_all_or_nothing() -> None:
    assert tasks_page._try_claim_bulk(INSTANCE, ["a", "b", "c"], "deleting") is True
    # One busy member refuses a second bulk action, and claims nothing.
    assert tasks_page._try_claim_bulk(INSTANCE, ["c", "d"], "deleting") is False
    assert operation_gate.run_claim(INSTANCE, "d") is None
    assert tasks_page._try_claim_bulk(INSTANCE, [], "deleting") is False


def test_bulk_release_frees_every_selected_run() -> None:
    assert tasks_page._try_claim_bulk(INSTANCE, ["a", "b"], "deleting") is True
    tasks_page._release_bulk_claim(INSTANCE, ["a", "b"])
    assert operation_gate.run_claim(INSTANCE, "a") is None
    assert operation_gate.run_claim(INSTANCE, "b") is None


def test_unbound_instance_cannot_claim_anything() -> None:
    assert tasks_page._try_claim_bulk("", ["a"], "deleting") is False
    assert tasks_page._run_is_busy("", "a") is False


def test_concurrent_bulk_claims_admit_exactly_one() -> None:
    """Thread-safety: N racing bulk claims over the same run admit one winner."""
    import threading

    barrier = threading.Barrier(8)
    results: list[bool] = []

    def racer() -> None:
        barrier.wait()
        results.append(tasks_page._try_claim_bulk(INSTANCE, ["shared"], "deleting"))

    threads = [threading.Thread(target=racer) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results.count(True) == 1
    assert operation_gate.run_claim(INSTANCE, "shared") is not None


def test_bulk_label_reads_naturally_for_one_and_many() -> None:
    assert tasks_page._bulk_label(1, "deleting") == "1 task is being deleted"
    assert tasks_page._bulk_label(3, "deleting") == "3 tasks are being deleted"
