"""Isolated unit tests for the pure ES-freshness settle helpers (Option A, rev 2).

No mocks, no network - literal ``/api/v2/search``-shaped dicts and raw entity
dicts only. Run only this file:

    .venv/bin/python -m pytest src/uwazi_admin_agent/tests/test_search_probe.py -v
"""

from uwazi_admin_agent.domain.search_probe import (
    build_freshness_result,
    entity_unchanged,
    extract_edit_date,
    format_freshness_warning,
    format_orphan_error,
    format_settle_blocked_error,
    identify_leftover_shared_ids,
    strict_settle_targets,
)

# --- extract_edit_date ----------------------------------------------------


def test_extract_edit_date_present():
    assert extract_edit_date({"data": [{"sharedId": "a", "editDate": 1000}]}) == 1000


def test_extract_edit_date_takes_max_across_hits():
    out = extract_edit_date({"data": [{"sharedId": "a", "editDate": 1000}, {"sharedId": "a", "editDate": 2000}]})
    assert out == 2000


def test_extract_edit_date_numeric_string_coerced():
    assert extract_edit_date({"data": [{"sharedId": "a", "editDate": "1500"}]}) == 1500


def test_extract_edit_date_empty_data():
    assert extract_edit_date({"data": []}) is None


def test_extract_edit_date_missing_data_key():
    assert extract_edit_date({"links": {}}) is None


def test_extract_edit_date_data_not_list():
    assert extract_edit_date({"data": {"editDate": 1000}}) is None


def test_extract_edit_date_skips_hit_without_edit_date():
    out = extract_edit_date({"data": [{"sharedId": "a"}, {"editDate": "nope"}, {"sharedId": "a", "editDate": 900}]})
    assert out == 900


def test_extract_edit_date_skips_non_dict_hit():
    out = extract_edit_date({"data": ["junk", 5, None, {"sharedId": "a", "editDate": 700}]})
    assert out == 700


def test_extract_edit_date_bool_rejected():
    assert extract_edit_date({"data": [{"sharedId": "a", "editDate": True}]}) is None


# --- build_freshness_result -----------------------------------------------


def test_build_freshness_result_all_fresh():
    r = build_freshness_result({"a": 100, "b": 200}, {"a": 100, "b": 250}, timed_out=False)
    assert r.all_fresh is True
    assert r.fresh_ids == ["a", "b"]
    assert r.pending_ids == []
    assert r.timed_out is False


def test_build_freshness_result_target_zero_means_visible():
    # target 0 = "editDate present" (post-create visibility settle)
    r = build_freshness_result({"a": 0}, {"a": 100}, timed_out=False)
    assert r.all_fresh is True


def test_build_freshness_result_partial():
    r = build_freshness_result({"a": 100, "b": 200, "c": 300}, {"a": 100, "b": 150, "c": 300}, timed_out=False)
    assert r.all_fresh is False
    assert r.fresh_ids == ["a", "c"]
    assert r.pending_ids == ["b"]


def test_build_freshness_result_none_observed_is_pending():
    r = build_freshness_result({"a": 100}, {"a": None}, timed_out=True)
    assert r.all_fresh is False
    assert r.pending_ids == ["a"]
    assert r.timed_out is True


def test_build_freshness_result_preserves_target_order():
    r = build_freshness_result({"c": 1, "a": 1, "b": 1}, {"c": 1, "a": None, "b": 1}, timed_out=False)
    assert r.fresh_ids == ["c", "b"]
    assert r.pending_ids == ["a"]


def test_build_freshness_result_empty_targets_all_fresh():
    r = build_freshness_result({}, {}, timed_out=False)
    assert r.all_fresh is True
    assert r.pending_ids == []


def test_build_freshness_result_timeout_flag_independent():
    r = build_freshness_result({"a": 100}, {"a": 100}, timed_out=True)
    assert r.all_fresh is True
    assert r.timed_out is True


def test_build_freshness_result_is_frozen():
    r = build_freshness_result({"a": 1}, {"a": 1}, timed_out=False)
    try:
        r.timed_out = True  # type: ignore[misc]
    except Exception:
        pass
    else:  # pragma: no cover - frozen models must raise
        raise AssertionError("FreshnessResult must be frozen")
    assert r.timed_out is False


# --- strict_settle_targets -------------------------------------------------


def test_strict_settle_targets_reverted_uses_pre_revert_plus_one():
    after = {"a": {"sharedId": "a", "editDate": 100}}
    post_revert = {"a": {"sharedId": "a", "editDate": 100}}  # collision: same editDate
    reverted = {"a"}
    assert strict_settle_targets(after, post_revert, reverted) == {"a": 101}


def test_strict_settle_targets_non_reverted_keeps_latest():
    after = {"a": {"sharedId": "a", "editDate": 100}}
    post_revert = {"a": {"sharedId": "a", "editDate": 100}}
    reverted = set()
    assert strict_settle_targets(after, post_revert, reverted) == {"a": 100}


def test_strict_settle_targets_script_created_visibility():
    after = {"new1": {"sharedId": "new1", "editDate": 500}}
    post_revert = {}
    reverted = set()
    assert strict_settle_targets(after, post_revert, reverted) == {"new1": 500}


def test_strict_settle_targets_recreated_new_shared_id():
    after = {"old1": None}  # deleted by script
    post_revert = {"old1": {"sharedId": "new1", "editDate": 500}}
    reverted = set()
    assert strict_settle_targets(after, post_revert, reverted) == {"new1": 500}


def test_strict_settle_targets_mixed():
    after = {
        "a": {"sharedId": "a", "editDate": 100},  # reverted
        "b": {"sharedId": "b", "editDate": 200},  # unchanged
        "c": {"sharedId": "c", "editDate": 300},  # script-created
    }
    post_revert = {
        "a": {"sharedId": "a", "editDate": 100},
        "b": {"sharedId": "b", "editDate": 200},
    }
    reverted = {"a"}
    assert strict_settle_targets(after, post_revert, reverted) == {"a": 101, "b": 200, "c": 300}


def test_strict_settle_targets_skips_missing_edit_date():
    after = {"a": {"sharedId": "a"}}  # no editDate
    post_revert = {}
    reverted = {"a"}
    assert strict_settle_targets(after, post_revert, reverted) == {}


def test_strict_settle_targets_empty():
    assert strict_settle_targets({}, {}, set()) == {}


# --- entity_unchanged -----------------------------------------------------


def test_entity_unchanged_identical():
    raw = {"sharedId": "a", "title": "t", "editDate": 100}
    assert entity_unchanged(raw, raw) is True


def test_entity_unchanged_ignores_platform_managed_editDate():
    before = {"sharedId": "a", "title": "t", "editDate": 100}
    after = {"sharedId": "a", "title": "t", "editDate": 999}  # only editDate differs
    assert entity_unchanged(before, after) is True


def test_entity_unchanged_detects_real_change():
    before = {"sharedId": "a", "title": "t", "editDate": 100}
    after = {"sharedId": "a", "title": "t-changed", "editDate": 999}
    assert entity_unchanged(before, after) is False


def test_entity_unchanged_detects_metadata_change():
    before = {"sharedId": "a", "metadata": {"p": [{"value": "x"}]}, "editDate": 100}
    after = {"sharedId": "a", "metadata": {"p": [{"value": "y"}]}, "editDate": 999}
    assert entity_unchanged(before, after) is False


# --- format_freshness_warning ---------------------------------------------


def test_format_freshness_warning_names_stage_and_pending():
    r = build_freshness_result({"a": 1, "b": 1, "c": 1}, {"a": 1, "b": None, "c": None}, timed_out=True)
    msg = format_freshness_warning("cleanup", r)
    assert "cleanup" in msg
    assert "2 of 3" in msg
    assert "b" in msg and "c" in msg


def test_format_freshness_warning_single_pending():
    r = build_freshness_result({"x9": 1}, {"x9": None}, timed_out=True)
    msg = format_freshness_warning("create", r)
    assert "create" in msg
    assert "1 of 1" in msg
    assert "x9" in msg


# --- identify_leftover_shared_ids -----------------------------------------


def test_identify_leftover_shared_ids_returns_present_ids():
    observed = {"a": 100, "b": None, "c": 300}
    assert identify_leftover_shared_ids(observed) == ["a", "c"]


def test_identify_leftover_shared_ids_all_gone():
    observed = {"a": None, "b": None}
    assert identify_leftover_shared_ids(observed) == []


def test_identify_leftover_shared_ids_empty():
    assert identify_leftover_shared_ids({}) == []


def test_identify_leftover_shared_ids_preserves_order():
    observed = {"c": 1, "a": None, "b": 2}
    assert identify_leftover_shared_ids(observed) == ["c", "b"]


# --- format_orphan_error ---------------------------------------------------


def test_format_orphan_error_names_leftover_ids():
    msg = format_orphan_error(["6tz8pi497j3", "abc123"])
    assert "2 orphan" in msg
    assert "6tz8pi497j3" in msg
    assert "abc123" in msg
    assert "reindex" in msg


def test_format_orphan_error_single():
    msg = format_orphan_error(["6tz8pi497j3"])
    assert "1 orphan" in msg
    assert "6tz8pi497j3" in msg


# --- format_settle_blocked_error -------------------------------------------


def test_format_settle_blocked_error_names_pending_ids():
    msg = format_settle_blocked_error(["a1", "b2"])
    assert "2 dummy" in msg
    assert "a1" in msg
    assert "b2" in msg
    assert "Mongo" in msg
    assert "recoverable" in msg


def test_format_settle_blocked_error_single():
    msg = format_settle_blocked_error(["a1"])
    assert "1 dummy" in msg
    assert "a1" in msg
