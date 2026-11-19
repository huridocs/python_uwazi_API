"""Isolated tests for the per-instance write gate and per-run claims.

Per AGENTS.md: no network, no mocks. Both registries live at module level and
are the state under test; every test scopes itself to its own instance URL and
releases what it took, so runs never interfere. The display helpers are pure.
"""

from __future__ import annotations

from uwazi_admin_agent.drivers.web.operation_gate import (
    GateState,
    RunClaim,
    active_text,
    busy_text,
    progress_text,
    release,
    release_run,
    release_runs,
    run_claim,
    run_claim_text,
    run_claims,
    set_note,
    set_progress,
    snapshot,
    try_acquire,
    try_claim_run,
    try_claim_runs,
)
from uwazi_admin_agent.use_cases.apply_suggestions_use_case import (
    apply_units_total,
    payload_units,
)

A = "http://instance-a.test"
B = "http://instance-b.test"


# --- Uwazi-write gate: one writer per instance --------------------------------


def test_one_write_per_instance_does_not_block_another_instance() -> None:
    try:
        assert try_acquire(A, "run", "r1") is True
        # A different instance writes at the same time.
        assert try_acquire(B, "run", "r2") is True
        # The SAME instance refuses any second write.
        assert try_acquire(A, "revert", "r3") is False
        assert try_acquire(A, "run", "r3") is False
        assert (snapshot(A).kind, snapshot(A).run_id) == ("run", "r1")
        assert (snapshot(B).kind, snapshot(B).run_id) == ("run", "r2")
    finally:
        release(A, "run", "r1")
        release(B, "run", "r2")


def test_an_unbound_instance_cannot_claim_and_has_no_snapshot() -> None:
    assert try_acquire("", "run", "r1") is False
    assert snapshot("") is None
    # Reporting against an unbound/unknown instance is a silent no-op.
    set_progress("", 1, 2)
    assert snapshot("") is None


def test_release_with_wrong_ownership_keeps_the_claim() -> None:
    try:
        assert try_acquire(A, "revert", "r9") is True
        # Another instance's release never touches this one.
        release(B, "revert", "r9")
        assert snapshot(A) is not None
        # A mismatched finish (another run, another kind) cannot free the slot.
        release(A, "revert", "other-run")
        release(A, "run", "r9")
        assert snapshot(A) is not None
        # The owner's own release frees it.
        release(A, "revert", "r9")
        assert snapshot(A) is None
    finally:
        release(A)


def test_release_is_idempotent_and_ownership_args_are_optional() -> None:
    release(A)  # idle release must not raise
    release("")
    release(A, "run", "missing")
    assert snapshot(A) is None
    try:
        try_acquire(A, "run", "r1")
        release(A)  # no ownership check: frees the slot
        assert snapshot(A) is None
    finally:
        release(A)


def test_set_progress_is_per_instance_and_ignored_when_idle() -> None:
    try:
        # A report with nothing held is dropped silently.
        set_progress(A, 3, 10)
        assert snapshot(A) is None
        try_acquire(A, "run", "r1")
        set_progress(A, 13, 1230)
        # Reporting to the OTHER instance does not touch this one.
        set_progress(B, 99, 100)
        busy = snapshot(A)
        assert busy is not None
        assert (busy.done, busy.total) == (13, 1230)
        assert busy.instance == A
    finally:
        release(A)


def test_set_progress_note_phases() -> None:
    try:
        try_acquire(A, "run", "r1")
        # Naming a phase: the note rides the snapshot and the fraction stays
        # indeterminate (no total yet).
        set_progress(A, 0, 0, "verifying the selection against Uwazi")
        busy = snapshot(A)
        assert busy is not None
        assert (busy.done, busy.total) == (0, 0)
        assert busy.note == "verifying the selection against Uwazi"
        # A plain fraction tick (no note argument) KEEPS the phase note: the
        # batch accounting must not wipe what the pass is telling the operator.
        set_progress(A, 1, 1)
        busy = snapshot(A)
        assert busy is not None
        assert (busy.done, busy.total) == (1, 1)
        assert busy.note == "verifying the selection against Uwazi"
        # A new note keeps the fraction and swaps the text.
        set_progress(A, 1, 1, "bulk write refused — applying per entity")
        busy = snapshot(A)
        assert busy is not None
        assert busy.note == "bulk write refused — applying per entity"
        # An empty note clears the phase.
        set_progress(A, 1, 1, "")
        busy = snapshot(A)
        assert busy is not None
        assert busy.note == ""
    finally:
        release(A, "run", "r1")


def test_set_note_publishes_without_moving_the_fraction() -> None:
    try:
        try_acquire(A, "run", "r1")
        set_progress(A, 7, 9)
        set_note(A, "bulk write refused — applying per entity")
        busy = snapshot(A)
        assert busy is not None
        assert (busy.done, busy.total) == (7, 9)
        assert busy.note == "bulk write refused — applying per entity"
        # A note for the other instance cannot land on this holder.
        set_note(B, "orphan")
        assert snapshot(B) is None
        assert snapshot(A).note == "bulk write refused — applying per entity"
    finally:
        release(A, "run", "r1")


# --- per-run claims -----------------------------------------------------------


def test_same_run_is_exclusive_but_different_runs_are_independent() -> None:
    try:
        assert try_claim_run(A, "r1", "creating") is True
        # The same run refuses another local operation.
        assert try_claim_run(A, "r1", "refresh") is False
        assert try_claim_run(A, "r1", "deleting") is False
        # Another run of the SAME instance stays free.
        assert try_claim_run(A, "r2", "refresh") is True
        # The same run id on ANOTHER instance stays free.
        assert try_claim_run(B, "r1", "run") is True
        assert run_claim(A, "r1").kind == "creating"
        assert run_claim(B, "r1").kind == "run"
    finally:
        release_runs(A, ["r1", "r2"])
        release_runs(B, ["r1"])


def test_run_claim_and_write_gate_are_independent_scopes() -> None:
    try:
        # A write path claims its run FIRST, then the instance write gate.
        assert try_claim_run(A, "r1", "run") is True
        assert try_acquire(A, "run", "r1") is True
        # A different run of the writing instance may still generate: it takes
        # no Uwazi write gate.
        assert try_claim_run(A, "r2", "creating") is True
        # But a second Uwazi write is refused for the whole instance.
        assert try_acquire(A, "revert", "r2") is False
    finally:
        release(A, "run", "r1")
        release_runs(A, ["r1", "r2"])


def test_run_claim_release_checks_ownership_and_is_idempotent() -> None:
    try:
        assert try_claim_run(A, "r1", "creating") is True
        release_run(A, "r1", "deleting")  # wrong kind: keep it
        assert run_claim(A, "r1") is not None
        release_run(B, "r1", "creating")  # wrong instance: keep it
        assert run_claim(A, "r1") is not None
        release_run(A, "r1", "creating")
        assert run_claim(A, "r1") is None
        release_run(A, "r1", "creating")  # idempotent
    finally:
        release_run(A, "r1")


def test_run_claim_rejects_an_unbound_instance_or_empty_run() -> None:
    assert try_claim_run("", "r1", "creating") is False
    assert try_claim_run(A, "", "creating") is False
    assert run_claim("", "r1") is None
    assert run_claims("") == ()


def test_bulk_claim_is_atomic_and_lists_every_taken_run() -> None:
    try:
        assert try_claim_run(A, "b", "refresh") is True
        # One busy member refuses the WHOLE bulk action; nothing is claimed.
        assert try_claim_runs(A, ["a", "b", "c"], "deleting") is False
        assert run_claim(A, "a") is None
        assert run_claim(A, "c") is None
        release_run(A, "b", "refresh")
        assert try_claim_runs(A, ["c", "a", "a"], "deleting") is True
        assert {claim.run_id for claim in run_claims(A)} == {"a", "c"}
    finally:
        release_runs(A, ["a", "b", "c"])


def test_bulk_release_only_frees_the_given_kind() -> None:
    try:
        assert try_claim_runs(A, ["a", "b"], "deleting") is True
        release_runs(A, ["a", "b"], "renaming")
        assert {claim.run_id for claim in run_claims(A)} == {"a", "b"}
        release_runs(A, ["a", "b"], "deleting")
        assert run_claims(A) == ()
    finally:
        release_runs(A, ["a", "b"])


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
    run = GateState(instance=A, kind="run", run_id="migration-7", done=13, total=1230, started_at=None)  # type: ignore[arg-type]
    assert active_text(run) == "Running — 13/1230 suboperations…"
    revert = GateState(instance=A, kind="revert", run_id="migration-7", done=5, total=40, started_at=None)  # type: ignore[arg-type]
    assert active_text(revert) == "Reverting — 5/40 entity…"
    # A non-gated kind has no unit; it degrades to the verb alone.
    odd = GateState(instance=A, kind="other", run_id="x", done=0, total=0, started_at=None)  # type: ignore[arg-type]
    assert active_text(odd) == "other…"
    assert active_text(None) == "Uwazi operation in progress…"


def test_active_text_appends_the_holder_note() -> None:
    stated = GateState(
        instance=A,
        kind="run",
        run_id="migration-7",
        done=0,
        total=0,
        started_at=None,  # type: ignore[arg-type]
        note="verifying the selection against Uwazi",
    )
    text = active_text(stated)
    assert "Running" in text
    assert "verifying the selection against Uwazi" in text
    refused = GateState(
        instance=A,
        kind="run",
        run_id="migration-7",
        done=1,
        total=2,
        started_at=None,  # type: ignore[arg-type]
        note="bulk write refused — applying per entity",
    )
    text = active_text(refused)
    assert "1/2 suboperations" in text
    assert "bulk write refused — applying per entity" in text


def test_busy_text_names_the_holder_and_its_live_count() -> None:
    run = GateState(instance=A, kind="run", run_id="migration-7", done=13, total=1230, started_at=None)  # type: ignore[arg-type]
    message = busy_text(run)
    assert "migration-7" in message
    assert "13/1230" in message
    assert "Wait until it finishes" in message
    fallback = busy_text(None)
    assert "Busy" in fallback and "in progress" in fallback


def test_run_claim_text_names_the_verb_and_the_run() -> None:
    claim = RunClaim(instance=A, run_id="migration-7", kind="creating", started_at=None)  # type: ignore[arg-type]
    text = run_claim_text(claim)
    assert "migration-7" in text
    assert "generating" in text
    assert "Wait until it finishes" in text
    assert "This task is busy" in run_claim_text(None)


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
