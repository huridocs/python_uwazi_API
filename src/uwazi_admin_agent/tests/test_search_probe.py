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
    max_edit_date_per_shared_id,
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


# --- max_edit_date_per_shared_id ------------------------------------------


def test_max_edit_date_keys_by_shared_id():
    raws = [
        {"sharedId": "a", "editDate": 100},
        {"sharedId": "b", "editDate": 50},
        {"sharedId": "a", "editDate": 300},  # re-index -> newer editDate
    ]
    assert max_edit_date_per_shared_id(raws) == {"a": 300, "b": 50}


def test_max_edit_date_skips_non_dict_and_missing():
    raws = [
        "junk",
        None,
        {"editDate": 100},  # no sharedId
        {"sharedId": "a"},  # no editDate
        {"sharedId": "a", "editDate": 200},
    ]
    assert max_edit_date_per_shared_id(raws) == {"a": 200}


def test_max_edit_date_handles_recreated_new_shared_id():
    # A re-created raw lives under the old id in post_revert but its sharedId is
    # the NEW id; keying by sharedId targets the new id (what ES indexes it under).
    raws = [
        {"sharedId": "old1", "editDate": 100},  # before (dead old id - excluded by caller)
        {"sharedId": "new1", "editDate": 500},  # post_revert re-created raw
    ]
    out = max_edit_date_per_shared_id(raws)
    assert out["new1"] == 500
    assert "old1" in out and out["old1"] == 100  # caller filters dead ids; helper just keys


def test_max_edit_date_empty():
    assert max_edit_date_per_shared_id([]) == {}


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
