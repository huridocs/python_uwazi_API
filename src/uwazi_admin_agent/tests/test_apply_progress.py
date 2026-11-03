"""Isolated unit tests for the apply pass's progress wrapper (per AGENTS.md).

Pure: a plain list callback in, recorded calls out. No mocks, no I/O, no
instance. The wrapper used to close over the name it was installed under and
call itself, so every tick raised ``maximum recursion depth exceeded`` and the
operator's real callback never ran.
"""

from uwazi_admin_agent.use_cases.apply_suggestions_use_case import track_progress


def test_tracked_progress_forwards_each_tick_to_the_original_callback() -> None:
    calls: list[tuple[int, int, str | None]] = []

    def callback(done: int, total: int, note: str | None = None) -> None:
        calls.append((done, total, note))

    units_so_far = [0]
    tracked = track_progress(callback, units_so_far)

    tracked(3, 9)
    tracked(4, 9, "note")

    assert calls == [(3, 9, None), (4, 9, "note")]
    assert units_so_far == [4]


def test_tracked_progress_without_a_callback_only_records_the_fraction() -> None:
    units_so_far = [0]
    tracked = track_progress(None, units_so_far)

    tracked(2, 5, "note")

    assert units_so_far == [2]
