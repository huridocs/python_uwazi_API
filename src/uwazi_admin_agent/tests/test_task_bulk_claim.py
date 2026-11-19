"""Isolated unit tests for the tasks table's BULK-delete claim (per AGENTS.md).

A bulk action claims every selected run atomically: it succeeds only when all
of them are free, and it never touches an unrelated run. Different runs stay
independent, so two operators can delete different tasks at the same time.

No mocks, no network: the helpers only touch the per-instance claim registry (no
clients are connected in a test, so the refresh broadcast is a no-op).
"""

from __future__ import annotations

import pytest

from uwazi_admin_agent.drivers.web import app as tasks_page
from uwazi_admin_agent.drivers.web import operation_gate

INSTANCE = "http://bulk-claim.test"


@pytest.fixture(autouse=True)
def _reset_claim():
    def _clear() -> None:
        for claim in operation_gate.run_claims(INSTANCE):
            operation_gate.release_run(INSTANCE, claim.run_id)

    _clear()
    yield
    _clear()


def test_bulk_claim_takes_every_selected_run_once() -> None:
    assert tasks_page._try_claim_bulk(INSTANCE, ["a", "b", "c"], "deleting") is True
    assert {claim.run_id for claim in operation_gate.run_claims(INSTANCE)} == {"a", "b", "c"}
    # All of them carry the bulk action's kind.
    assert {claim.kind for claim in operation_gate.run_claims(INSTANCE)} == {"deleting"}


def test_an_unrelated_run_does_not_block_the_bulk_action() -> None:
    assert operation_gate.try_claim_run(INSTANCE, "busy", "refresh") is True
    assert tasks_page._try_claim_bulk(INSTANCE, ["a", "b"], "deleting") is True


def test_one_busy_selected_run_refuses_the_whole_action() -> None:
    assert operation_gate.try_claim_run(INSTANCE, "b", "creating") is True
    assert tasks_page._try_claim_bulk(INSTANCE, ["a", "b"], "deleting") is False
    # Atomic: the free run was not claimed either.
    assert operation_gate.run_claim(INSTANCE, "a") is None


def test_releasing_the_bulk_claim_clears_the_selected_runs() -> None:
    assert tasks_page._try_claim_bulk(INSTANCE, ["a", "b"], "deleting") is True
    tasks_page._release_bulk_claim(INSTANCE, ["a", "b"])
    assert operation_gate.run_claims(INSTANCE) == ()


def test_release_with_a_different_set_leaves_the_claim_alone() -> None:
    assert tasks_page._try_claim_bulk(INSTANCE, ["a", "b"], "deleting") is True
    tasks_page._release_bulk_claim(INSTANCE, ["a"])
    assert operation_gate.run_claim(INSTANCE, "b") is not None


def test_bulk_label_reads_naturally_for_one_and_many() -> None:
    assert tasks_page._bulk_label(1, "deleting") == "1 task is being deleted"
    assert tasks_page._bulk_label(3, "deleting") == "3 tasks are being deleted"
