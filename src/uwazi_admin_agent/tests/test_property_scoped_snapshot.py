"""Isolated unit tests for the property-scoped snapshot transforms (pure)."""

from __future__ import annotations

from datetime import datetime, timezone

from uwazi_admin_agent.domain.property_restore import (
    bulk_property_groups,
    can_bulk_update,
    keep_file_entries,
    merge_touched_properties,
    minimal_property_row,
    snapshot_restore_payload,
)
from uwazi_admin_agent.domain.snapshot import EntityRowSnapshot, EntitySnapshot


def _raw(**overrides: object) -> dict:
    raw = {
        "_id": "internal-1",
        "sharedId": "entity-a",
        "language": "en",
        "title": "A title",
        "template": "template-1",
        "metadata": {
            "a": [{"value": "old a"}],
            "b": [{"value": "old b"}],
        },
        "relations": [{"hub": "x"}],
        "permissions": [{"refId": "u1"}],
    }
    raw.update(overrides)
    return raw


def test_minimal_row_keeps_identity_title_and_only_the_touched_keys() -> None:
    row = minimal_property_row(_raw(), "en", ["a"])

    assert row is not None
    assert row.raw["_id"] == "internal-1"
    assert row.raw["sharedId"] == "entity-a"
    assert row.raw["language"] == "en"
    assert row.raw["title"] == "A title"
    assert row.raw["template"] == "template-1"
    assert row.raw["metadata"] == {"a": [{"value": "old a"}]}
    # The cheap shape: no relations, no permissions, no untouched properties.
    assert "relations" not in row.raw
    assert "permissions" not in row.raw
    assert row.touched_properties == ["a"]


def test_a_key_absent_before_is_touched_but_not_captured() -> None:
    row = minimal_property_row(_raw(), "en", ["brand_new"])

    assert row is not None
    assert row.touched_properties == ["brand_new"]
    assert row.raw["metadata"] == {}


def test_a_raw_without_id_or_title_cannot_be_property_scoped() -> None:
    assert minimal_property_row(_raw(_id=""), "en", ["a"]) is None
    assert minimal_property_row(_raw(title=""), "en", ["a"]) is None
    assert minimal_property_row(_raw(title=None), "en", ["a"]) is None


def test_touched_properties_are_deduplicated_in_order() -> None:
    row = minimal_property_row(_raw(), "en", ["a", "a", "b"])

    assert row is not None
    assert row.touched_properties == ["a", "b"]


def test_merging_a_new_key_keeps_the_first_captured_value() -> None:
    first = minimal_property_row(_raw(), "en", ["a"])
    assert first is not None

    live = _raw(metadata={"a": [{"value": "written by the run"}], "b": [{"value": "live b"}]})

    merged = merge_touched_properties(first, live, ["a", "b"])

    assert merged.touched_properties == ["a", "b"]
    # The pre-ANY-write value of "a" survives; "b" is captured live (never written yet).
    assert merged.raw["metadata"]["a"] == [{"value": "old a"}]
    assert merged.raw["metadata"]["b"] == [{"value": "live b"}]


def test_merging_nothing_returns_the_same_row() -> None:
    row = minimal_property_row(_raw(), "en", ["a"])
    assert row is not None

    assert merge_touched_properties(row, _raw(), ["a"]) is row


def test_restore_payload_carries_only_the_touched_keys_and_clears_absent_ones() -> None:
    row = minimal_property_row(_raw(), "en", ["a", "brand_new"])
    assert row is not None

    payload = snapshot_restore_payload("entity-a", row)

    assert payload["_id"] == "internal-1"
    assert payload["sharedId"] == "entity-a"
    assert payload["language"] == "en"
    assert payload["title"] == "A title"
    assert payload["metadata"] == {"a": [{"value": "old a"}], "brand_new": []}
    # The template is grouping metadata, never part of a restore body.
    assert "template" not in payload


def _plan(shared_id: str, **overrides: object) -> dict:
    plan = {
        "shared_id": shared_id,
        "language": "en",
        "template": "template-1",
        "title_changed": False,
        "metadata": {"a": [{"value": "same"}]},
        "body": {},
    }
    plan.update(overrides)
    return plan


def test_identical_plans_group_into_one_bulk_call() -> None:
    groups = bulk_property_groups([_plan("a"), _plan("b"), _plan("c")])

    assert len(groups) == 1
    assert [plan["shared_id"] for plan in groups[0]] == ["a", "b", "c"]


def test_different_values_templates_or_languages_never_share_a_group() -> None:
    plans = [
        _plan("a"),
        _plan("b", metadata={"a": [{"value": "other"}]}),
        _plan("c", template="template-2"),
        _plan("d", language="es"),
    ]

    groups = bulk_property_groups(plans)

    assert [len(group) for group in groups] == [1, 1, 1, 1]


def test_a_title_write_cannot_ride_a_bulk_call() -> None:
    groups = bulk_property_groups([_plan("a", title_changed=True), _plan("b", title_changed=True)])

    assert [len(group) for group in groups] == [1, 1]


def test_a_missing_template_keeps_the_plan_on_the_per_entity_path() -> None:
    groups = bulk_property_groups([_plan("a", template=None), _plan("b", template=None)])

    assert [len(group) for group in groups] == [1, 1]


def test_an_empty_metadata_payload_is_never_bulk_written() -> None:
    groups = bulk_property_groups([_plan("a", metadata={}), _plan("b", metadata={})])

    assert [len(group) for group in groups] == [1, 1]


def test_grouping_ignores_key_order_inside_the_metadata() -> None:
    left = _plan("a", metadata={"a": [{"value": "x"}], "b": [{"value": "y"}]})
    right = _plan("b", metadata={"b": [{"value": "y"}], "a": [{"value": "x"}]})

    groups = bulk_property_groups([left, right])

    assert len(groups) == 1


def test_a_lone_plan_is_still_bulk_written() -> None:
    # The bulk endpoint is file-safe; the per-entity POST deletes unlisted files.
    groups = bulk_property_groups([_plan("a")])

    assert len(groups) == 1
    assert can_bulk_update(groups[0][0]) is True


def test_the_bulk_predicate_refuses_exactly_what_the_endpoint_cannot_express() -> None:
    assert can_bulk_update(_plan("a")) is True
    assert can_bulk_update(_plan("a", title_changed=True)) is False
    assert can_bulk_update(_plan("a", template=None)) is False
    assert can_bulk_update(_plan("a", language=None)) is False
    assert can_bulk_update(_plan("a", metadata={})) is False


def test_keep_file_entries_echoes_ids_and_names_only() -> None:
    raw = {
        "documents": [
            {"_id": "doc-1", "originalname": "a.pdf", "filename": "123.pdf", "size": 10},
            {"originalname": "no-id.pdf"},
        ],
        "attachments": [
            {"_id": "att-1", "originalname": "b.html", "filename": "456.html"},
            {"originalname": "url-only.html"},
        ],
    }

    kept = keep_file_entries(raw)

    assert kept["documents"] == [{"_id": "doc-1", "originalname": "a.pdf"}]
    assert kept["attachments"] == [
        {"_id": "att-1", "originalname": "b.html"},
        {"originalname": "url-only.html"},
    ]


def test_keep_file_entries_is_empty_for_an_entity_without_files() -> None:
    assert keep_file_entries({"documents": [], "attachments": []}) == {"documents": [], "attachments": []}
    assert keep_file_entries(None) == {"documents": [], "attachments": []}


def test_a_property_scoped_snapshot_defaults_to_the_property_scope() -> None:
    row = minimal_property_row(_raw(), "en", ["a"])
    assert row is not None
    snapshot = EntitySnapshot(
        shared_id="entity-a",
        raw=row.raw,
        captured_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        scope="property",
        rows=[row],
    )

    assert snapshot.scope == "property"
    assert isinstance(snapshot.rows[0], EntityRowSnapshot)
    assert snapshot.rows[0].touched_properties == ["a"]
