"""Isolated unit tests for the tasks table's BULK-delete claim (per AGENTS.md).

The bulk action must take the ONE global in-flight slot ONCE for the whole set.
Claiming per run was the bug that made multi-select delete impossible: the
first claim succeeded and every later one was refused, so the operator saw a
"busy" toast for a page that was idle.

No mocks, no network: the helpers only touch the module's claim state and
broadcast to the connected clients (none in a test).
"""

from __future__ import annotations

import pytest

from uwazi_admin_agent.drivers.web import app as tasks_page


@pytest.fixture(autouse=True)
def _reset_claim():
    tasks_page._active_op = None
    yield
    tasks_page._active_op = None


def test_bulk_claim_takes_the_slot_once_for_the_whole_set() -> None:
    assert tasks_page._try_claim_bulk(["a", "b", "c"], "deleting") is True
    assert tasks_page._active_op is not None
    assert tasks_page._active_op["run_ids"] == ("a", "b", "c")


def test_a_second_claim_is_refused_while_the_bulk_holds_the_slot() -> None:
    assert tasks_page._try_claim_bulk(["a", "b"], "deleting") is True
    assert tasks_page._try_claim_bulk(["c"], "deleting") is False


def test_releasing_the_bulk_claim_clears_the_slot() -> None:
    assert tasks_page._try_claim_bulk(["a", "b"], "deleting") is True
    tasks_page._release_bulk_claim(["a", "b"])
    assert tasks_page._active_op is None


def test_release_with_a_different_set_leaves_the_claim_alone() -> None:
    assert tasks_page._try_claim_bulk(["a", "b"], "deleting") is True
    tasks_page._release_bulk_claim(["a"])
    assert tasks_page._active_op is not None


def test_bulk_label_reads_naturally_for_one_and_many() -> None:
    assert tasks_page._bulk_label(1, "deleting") == "1 task is being deleted"
    assert tasks_page._bulk_label(3, "deleting") == "3 tasks are being deleted"
