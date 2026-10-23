"""Isolated tests for the Uwazi-write operation gate (per AGENTS.md).

No network, no mocks of behavior under test: the gate itself is an in-memory
process-global (its registry is the state under test) and the Run's progress
counting is pure arithmetic over literal payloads.
"""

from __future__ import annotations

from uwazi_admin_agent.drivers.web.operation_gate import (
    GateState,
    active_text,
    busy_text,
    progress_text,
    release,
    set_progress,
    snapshot,
    try_acquire,
)
from uwazi_admin_agent.use_cases.apply_suggestions_use_case import (
    apply_units_total,
    payload_units,
)


def _reset_gate() -> None:
    """Force the process-global gate to its idle state between tests."""
    release()


# --- gate lifecycle ----------------------------------------------------------


def test_try_acquire_claims_the_single_slot() -> None:
    _reset_gate()
    try:
        assert try_acquire("run", "r1") is True
        # A second claim (any kind, any run) is refused while one is held.
        assert try_acquire("revert", "r2") is False
        busy = snapshot()
        assert busy is not None
        assert (busy.kind, busy.run_id) == ("run", "r1")
        # A fresh claim carries zeroed progress until the worker ticks it.
        assert (busy.done, busy.total) == (0, 0)
    finally:
        _reset_gate()


def test_release_with_wrong_ownership_keeps_the_claim() -> None:
    _reset_gate()
    try:
        try_acquire("revert", "r9")
        # A mismatched finish (another run, another kind) cannot free the slot.
        release("revert", "other-run")
        release("run", "r9")
        assert snapshot() is not None
        # The owner's own release frees it.
        release("revert", "r9")
        assert snapshot() is None
    finally:
        _reset_gate()


def test_release_is_idempotent() -> None:
    _reset_gate()
    release()  # idle release must not raise
    release("run", "missing")
    assert snapshot() is None


def test_set_progress_is_visible_in_the_snapshot_and_ignored_when_idle() -> None:
    _reset_gate()
    try:
        # A report with nothing held is dropped silently.
        set_progress(3, 10)
        assert snapshot() is None
        try_acquire("run", "r1")
        set_progress(13, 1230)
        busy = snapshot()
        assert busy is not None
        assert (busy.done, busy.total) == (13, 1230)
        assert busy.started_at is not None
    finally:
        _reset_gate()


# --- pure display helpers ----------------------------------------------------


def test_progress_text_reads_the_fraction() -> None:
    assert progress_text(13, 1230) == "13/1230"
    assert progress_text(1230, 1230) == "1230/1230"
    # An unknown total is an indeterminate pass: dots, never a bogus fraction.
    assert progress_text(0, 0) == "…"
    assert progress_text(5, 0) == "…"
    # Guards: done can never display past its total.
    assert progress_text(2000, 10) == "10/10"
    assert progress_text(-1, 10) == "0/10"


def test_active_text_names_the_operation_and_its_units() -> None:
    run = GateState(kind="run", run_id="migration-7", done=13, total=1230, started_at=None)  # type: ignore[arg-type]
    assert active_text(run) == "Running — 13/1230 suboperations…"
    revert = GateState(kind="revert", run_id="migration-7", done=5, total=40, started_at=None)  # type: ignore[arg-type]
    assert active_text(revert) == "Reverting — 5/40 entity…"
    # An indeterminate pass (no total yet) degrades to the verb alone.
    creating = GateState(kind="creating", run_id="new-task", done=0, total=0, started_at=None)  # type: ignore[arg-type]
    assert active_text(creating) == "Generating…"
    assert active_text(None) == "Uwazi operation in progress…"


def test_busy_text_names_the_holder_and_its_live_count() -> None:
    run = GateState(kind="run", run_id="migration-7", done=13, total=1230, started_at=None)  # type: ignore[arg-type]
    message = busy_text(run)
    assert "migration-7" in message
    assert "13/1230" in message
    assert "Wait until it finishes" in message
    fallback = busy_text(None)
    assert "Busy" in fallback and "in progress" in fallback


# --- Run progress arithmetic (pure) ------------------------------------------


def test_payload_units_counts_metadata_and_title_rows() -> None:
    payload = {"shared_id": "s1", "language": "es", "metadata": {"a": 1, "b": 2}, "title": "T"}
    assert payload_units(payload) == 3  # two property rows + the title row
    bare = {"shared_id": "s2", "language": "es", "metadata": {}}
    assert payload_units(bare) == 1  # a payload with nothing still counts one write


def test_apply_units_total_counts_rows_creates_and_merge_sources() -> None:
    updates = [
        {"metadata": {"a": 1, "b": 2}, "title": "T"},
        {"metadata": {}},
    ]
    creates = [{"metadata": {"c": 3}}]
    whole_rows = [_fake_row() for _ in range(3)]  # delete/publish/relationship rows
    merges = [("target-1", ["src-1", "src-2"])]
    total = apply_units_total(updates, creates, whole_rows, merges)
    # 3 (update payload) + 1 (bare update) + 1 (create) + 3 whole-entity + 2 merge sources.
    assert total == 10


def test_apply_units_total_without_merges_or_whole_rows() -> None:
    assert apply_units_total([], [], [], []) == 0


def _fake_row() -> object:
    """One whole-entity suggestion stand-in: only its count enters the total."""

    class _Row:
        pass

    return _Row()
