from typing import Any

from uwazi_admin_agent.domain.validation_result import (
    EntityDiff,
    RestoreMismatch,
    build_validation_outcome,
)


def _raw(title: str, **extra: Any) -> dict[str, Any]:
    return {"_id": "x", "sharedId": "S1", "title": title, "language": "en", **extra}


# --- EntityDiff.changed ------------------------------------------------------


def test_entity_diff_unchanged_is_not_changed() -> None:
    raw = _raw("A")
    diff = EntityDiff(shared_id="S1", before=raw, after=raw)
    assert diff.changed is False


def test_entity_diff_modified_is_changed() -> None:
    diff = EntityDiff(shared_id="S1", before=_raw("A"), after=_raw("A-edited"))
    assert diff.changed is True


def test_entity_diff_created_is_changed() -> None:
    diff = EntityDiff(shared_id="S1", before=None, after=_raw("new"))
    assert diff.changed is True


def test_entity_diff_deleted_is_changed() -> None:
    diff = EntityDiff(shared_id="S1", before=_raw("A"), after=None)
    assert diff.changed is True


# --- build_validation_outcome: clean pass ------------------------------------


def test_clean_run_and_exact_restore_passes() -> None:
    before = {"S1": _raw("A"), "S2": _raw("B")}
    after = {"S1": _raw("A-edited"), "S2": _raw("B-edited")}
    post_revert = {"S1": _raw("A"), "S2": _raw("B")}

    result = build_validation_outcome(
        script_result="updated 2", script_error=None, before=before, after=after, post_revert=post_revert
    )

    assert result.passed is True
    assert result.script_result == "updated 2"
    assert result.script_error is None
    assert result.restore_equal is True
    assert result.restore_mismatches == []
    assert len(result.diffs) == 2
    assert all(d.changed for d in result.diffs)


def test_no_change_run_still_passes_with_empty_diffs() -> None:
    before = {"S1": _raw("A")}
    after = {"S1": _raw("A")}
    post_revert = {"S1": _raw("A")}

    result = build_validation_outcome("noop", None, before, after, post_revert)

    assert result.passed is True
    assert result.restore_equal is True
    assert [d.changed for d in result.diffs] == [False]


# --- script error -----------------------------------------------------------


def test_script_error_fails_even_if_restore_equal() -> None:
    before = {"S1": _raw("A")}
    after = {"S1": _raw("A-edited")}
    post_revert = {"S1": _raw("A")}  # restore happened to be exact

    result = build_validation_outcome(
        script_result=None, script_error="ValueError: bad", before=before, after=after, post_revert=post_revert
    )

    assert result.passed is False
    assert result.script_error == "ValueError: bad"
    assert result.script_result is None
    # restore_equal is reported independently of the script-error decision
    assert result.restore_equal is True


# --- restore mismatch -------------------------------------------------------


def test_restore_mismatch_fails_and_records_mismatch() -> None:
    before = {"S1": _raw("A", relations=[{"_id": "r1"}])}
    after = {"S1": _raw("A-edited")}
    post_revert = {"S1": _raw("A-edited")}  # revert did NOT restore original

    result = build_validation_outcome("done", None, before, after, post_revert)

    assert result.passed is False
    assert result.restore_equal is False
    assert len(result.restore_mismatches) == 1
    m = result.restore_mismatches[0]
    assert m.shared_id == "S1"
    assert m.expected == before["S1"]
    assert m.actual == post_revert["S1"]


# --- platform-managed fields (editDate) are excluded from restore-equality ----


def test_edit_date_only_difference_does_not_fail_restore() -> None:
    # Uwazi bumps editDate on every save, including the revert save, so an
    # editDate-only difference must NOT count as a restore mismatch.
    before = {"S1": _raw("A", editDate=1786964800475)}
    after = {"S1": _raw("A-edited", editDate=1786964800500)}
    post_revert = {"S1": _raw("A", editDate=1786964800531)}  # data restored, editDate advanced

    result = build_validation_outcome("done", None, before, after, post_revert)

    assert result.passed is True
    assert result.restore_equal is True
    assert result.restore_mismatches == []


def test_real_data_mismatch_plus_edit_date_difference_is_caught() -> None:
    before = {"S1": _raw("A", editDate=1786964800475)}
    after = {"S1": _raw("A-edited", editDate=1786964800500)}
    # title NOT restored (still A-edited) AND editDate advanced: real mismatch must surface.
    post_revert = {"S1": _raw("A-edited", editDate=1786964800531)}

    result = build_validation_outcome("done", None, before, after, post_revert)

    assert result.passed is False
    assert result.restore_equal is False
    assert len(result.restore_mismatches) == 1
    # The recorded mismatch carries the FULL raws (incl. editDate) for diagnostics.
    m = result.restore_mismatches[0]
    assert m.expected == before["S1"]
    assert m.actual == post_revert["S1"]


def test_platform_managed_fields_constant_is_edit_date_only() -> None:
    from uwazi_admin_agent.domain.validation_result import PLATFORM_MANAGED_FIELDS

    assert PLATFORM_MANAGED_FIELDS == frozenset({"editDate"})


def test_revert_failed_to_restore_entity_records_none_actual() -> None:
    before = {"S1": _raw("A")}
    after = {"S1": None}  # script deleted it
    post_revert = {"S1": None}  # revert failed to bring it back

    result = build_validation_outcome("deleted", None, before, after, post_revert)

    assert result.passed is False
    assert result.restore_equal is False
    assert result.restore_mismatches[0].actual is None


# --- created dummies excluded from restore check ----------------------------


def test_created_dummies_are_diffed_but_not_restore_checked() -> None:
    before = {"S1": _raw("A")}  # original
    after = {"S1": _raw("A-edited"), "S2": _raw("new")}  # S2 created by script
    post_revert = {"S1": _raw("A")}  # only original S1 reverted

    result = build_validation_outcome("done", None, before, after, post_revert, created_shared_ids=["S2"])

    assert result.passed is True  # S1 restored exactly; S2 has no before to check
    assert result.restore_equal is True
    assert result.created_shared_ids == ["S2"]
    by_id = {d.shared_id: d for d in result.diffs}
    assert by_id["S2"].before is None and by_id["S2"].after == _raw("new")
    assert by_id["S1"].changed is True


# --- deleted original is diffed and restore-checked -------------------------


def test_deleted_original_is_diffed_and_restore_checked() -> None:
    before = {"S1": _raw("A")}
    after = {"S1": None}  # script deleted it
    post_revert = {"S1": _raw("A")}  # revert re-created it exactly

    result = build_validation_outcome("deleted 1", None, before, after, post_revert)

    assert result.passed is True
    assert result.restore_equal is True
    assert result.diffs[0].before == _raw("A")
    assert result.diffs[0].after is None


# --- defaults ---------------------------------------------------------------


def test_empty_run_passes_with_defaults() -> None:
    result = build_validation_outcome(None, None, {}, {}, {})
    assert result.passed is True
    assert result.diffs == []
    assert result.restore_mismatches == []
    assert result.created_shared_ids == []
    assert result.cleanup_error is None


def test_cleanup_error_field_present() -> None:
    result = build_validation_outcome("done", None, {"S1": _raw("A")}, {"S1": _raw("A")}, {"S1": _raw("A")})
    assert result.cleanup_error is None  # set by the harness, not the pure builder


def test_restore_mismatch_model_is_frozen() -> None:
    m = RestoreMismatch(shared_id="S1", expected=_raw("A"), actual=_raw("B"))
    import pytest

    with pytest.raises(Exception):
        m.shared_id = "X"  # type: ignore[misc]


def test_validation_result_is_mutable_for_harness_update() -> None:
    result = build_validation_outcome("done", None, {"S1": _raw("A")}, {"S1": _raw("A")}, {"S1": _raw("A")})
    # The harness uses model_copy(update=...) to attach cleanup_error; the model
    # itself is mutable (not frozen) to allow that.
    updated = result.model_copy(update={"cleanup_error": "boom"})
    assert updated.cleanup_error == "boom"
    assert result.cleanup_error is None
