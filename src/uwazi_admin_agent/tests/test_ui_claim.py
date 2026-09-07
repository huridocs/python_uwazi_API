"""Isolated tests for the web UI's global mutating-op claim helpers.

Per AGENTS.md: pure, offline, no mocks. The claim state lives at module level
in ``app.py``; the tests import the module and drive the real helpers against
the real module-global state (reset between tests via the module attributes —
no monkeypatching of behavior, just plain assignments before each test).
"""

import threading

import pytest

from uwazi_admin_agent.drivers.web import app as web_app


@pytest.fixture(autouse=True)
def _reset_claim_state():
    """Start every test from an idle claim table (plain attribute reset)."""
    web_app._active_op = None
    web_app._running_runs.clear()
    yield
    web_app._active_op = None
    web_app._running_runs.clear()


def test_claim_succeeds_when_free() -> None:
    assert web_app._try_claim("run-1", "running") is True
    assert web_app._is_busy() is True
    assert web_app._active_op == {"run_id": "run-1", "kind": "running"}
    # Run-level ops register the transient status overlay for the table.
    assert web_app._running_runs == {"run-1": "running"}


def test_second_claim_fails_while_held() -> None:
    assert web_app._try_claim("run-1", "running") is True
    web_app._release_run("run-other")
    assert web_app._is_busy() is True
    assert web_app._active_op == {"run_id": "run-1", "kind": "running"}


def test_release_frees_the_claim() -> None:
    assert web_app._try_claim("run-1", "reverting") is True
    web_app._release_run("run-1")
    assert web_app._is_busy() is False
    assert web_app._active_op is None
    assert "run-1" not in web_app._running_runs
    # Freed slot is claimable again.
    assert web_app._try_claim("run-2", "creating") is True


def test_release_with_wrong_id_is_a_noop() -> None:
    assert web_app._try_claim("run-1", "running") is True
    web_app._release_run("run-other")
    assert web_app._is_busy() is True
    assert web_app._active_op == {"run_id": "run-1", "kind": "running"}


def test_release_on_idle_claim_is_a_noop() -> None:
    web_app._release_run("run-1")
    assert web_app._is_busy() is False


def test_busy_label_names_the_active_task() -> None:
    assert web_app._busy_label() == ""
    web_app._try_claim("merge-dupes", "reverting")
    assert web_app._busy_label() == "Task 'merge-dupes' is reverting"
    web_app._release_run("merge-dupes")
    web_app._try_claim("bulk-publish", "running")
    assert web_app._busy_label() == "Task 'bulk-publish' is running"


def test_creating_claim_overlays_creating_status() -> None:
    assert web_app._try_claim("new-run", "creating") is True
    assert web_app._running_runs == {"new-run": "creating"}


def test_rename_claim_does_not_overlay_row_status() -> None:
    assert web_app._try_claim("run-1", "renaming") is True
    assert web_app._running_runs == {}
    assert web_app._busy_label() == "Task 'run-1' is being renamed"


def test_concurrent_claims_admit_exactly_one() -> None:
    """Thread-safety: N racing claims must admit exactly one winner."""
    barrier = threading.Barrier(8)
    results: list[bool] = []

    def racer() -> None:
        barrier.wait()
        results.append(web_app._try_claim(f"run-{threading.get_ident()}", "running"))

    threads = [threading.Thread(target=racer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(True) == 1
    assert web_app._is_busy() is True
