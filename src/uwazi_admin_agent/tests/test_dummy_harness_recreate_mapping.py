"""Pure tests for the dummy-harness re-create mapping (Part 1 gate fix).

The harness itself needs the real instance and is not unit-tested, but the
re-create id mapping it relies on is a pure seam (:func:`resolve_recreated_fetch_ids`)
extracted from :class:`DummyEntityHarness._revert_originals`. These tests cover
it with literal inputs so the gate's deleted-original handling is verified
without touching Uwazi.
"""

from uwazi_admin_agent.use_cases.dummy_entity_harness import resolve_recreated_fetch_ids


def test_returns_old_id_when_not_recreated() -> None:
    # An original the script did NOT delete (or revert restored via save_raw) has
    # no entry in `recreated` -> fetch by its old id.
    assert resolve_recreated_fetch_ids(["S1", "S2"], {}) == [("S1", "S1"), ("S2", "S2")]


def test_returns_new_id_when_recreated() -> None:
    # An original the script deleted AND revert re-created fetches by the NEW id.
    recreated = {"S2": "NEW2"}
    assert resolve_recreated_fetch_ids(["S1", "S2", "S3"], recreated) == [
        ("S1", "S1"),
        ("S2", "NEW2"),
        ("S3", "S3"),
    ]


def test_preserves_order_and_keys_results_by_old_id() -> None:
    recreated = {"S3": "N3", "S1": "N1"}
    out = resolve_recreated_fetch_ids(["S1", "S2", "S3"], recreated)
    # The fetch plan is a list of (old_id, fetch_id); the caller keys post_revert
    # by old_id. Order follows the input old_ids.
    assert out == [("S1", "N1"), ("S2", "S2"), ("S3", "N3")]


def test_empty_inputs_yield_empty_plan() -> None:
    assert resolve_recreated_fetch_ids([], {}) == []


def test_does_not_mutate_inputs() -> None:
    old_ids = ["S1", "S2"]
    recreated = {"S2": "N2"}
    _ = resolve_recreated_fetch_ids(old_ids, recreated)
    assert old_ids == ["S1", "S2"]  # input list untouched
    assert recreated == {"S2": "N2"}  # input dict untouched
