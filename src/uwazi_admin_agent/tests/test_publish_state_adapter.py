"""Isolated unit tests for the publish-state query parameters (pure)."""

from __future__ import annotations

from uwazi_admin_agent.adapters.publish_state_adapter import read_params


def test_include_is_sent_as_an_array() -> None:
    """Uwazi's /api/entities schema requires ``include`` to be an array.

    A scalar ``include=permissions`` is rejected with a 400
    (``/query/include must be array``), which left snapshots without their
    publish state. The ``include[]`` key is the array encoding ``qs`` parses.
    """
    params = read_params("abc123")
    assert ("include[]", "permissions") in params
    assert all(key != "include" for key, _value in params)


def test_the_query_scopes_to_the_entity_and_skips_relationships() -> None:
    params = dict(read_params("abc123"))
    assert params["sharedId"] == "abc123"
    assert params["omitRelationships"] == "true"
