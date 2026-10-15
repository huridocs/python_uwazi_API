"""Isolated unit tests for record→suggestion conversion + accuracy scoring (per AGENTS.md).

Pure: literal dry-run record dicts in, real :class:`Suggestion` rows out. No
mocks, no I/O, no instance.
"""

from uwazi_admin_agent.domain.suggestion import TITLE_PROPERTY, Suggestion, SuggestionStatus
from uwazi_admin_agent.domain.suggestion_scoring import (
    compare_values,
    records_to_suggestions,
    score_accuracy,
)


def _rec(**kwargs: object) -> dict[str, object]:
    return dict(kwargs)


# --- records_to_suggestions: update ------------------------------------------


def test_update_record_explodes_per_metadata_key() -> None:
    records = [
        _rec(
            op="update",
            shared_id="s1",
            metadata={"excerpt": [{"value": "v1"}], "topic": ["T"]},
            title=None,
            template_name="Case",
            language="en",
        )
    ]
    rows = records_to_suggestions("run1", 1, records)
    assert [(r.op, r.property_name, r.shared_id) for r in rows] == [
        ("update", "excerpt", "s1"),
        ("update", "topic", "s1"),
    ]
    assert rows[0].proposed_value == [{"value": "v1"}]
    assert all(r.status == SuggestionStatus.PENDING for r in rows)
    assert all(r.run_id == "run1" and r.attempt == 1 for r in rows)
    assert all(r.created_at is not None for r in rows)


def test_update_record_with_title_emits_title_row_first() -> None:
    records = [_rec(op="update", shared_id="s1", metadata={"a": 1}, title="New Title")]
    rows = records_to_suggestions("run1", 2, records)
    assert [(r.property_name, r.proposed_value) for r in rows] == [
        (TITLE_PROPERTY, "New Title"),
        ("a", 1),
    ]


def test_update_record_with_no_metadata_and_no_title_emits_a_single_placeholder_row() -> None:
    records = [_rec(op="update", shared_id="s1", metadata={}, title=None)]
    rows = records_to_suggestions("run1", 1, records)
    assert len(rows) == 1
    assert rows[0].property_name is None
    assert rows[0].proposed_value is None


def test_update_record_with_missing_metadata_key_still_emits_placeholder() -> None:
    rows = records_to_suggestions("run1", 1, [_rec(op="update", shared_id="s1")])
    assert [r.property_name for r in rows] == [None]


# --- records_to_suggestions: create ------------------------------------------


def test_create_record_uses_placeholder_id_and_explodes_metadata() -> None:
    records = [
        _rec(op="create", title="Made Up", template_name="Case", metadata={"a": 1, "b": 2}),
        _rec(op="create", title="Second", template_name="Case", metadata={}),
    ]
    rows = records_to_suggestions("run1", 1, records)
    assert [(r.op, r.shared_id, r.property_name) for r in rows] == [
        ("create", "dry-created-0", TITLE_PROPERTY),
        ("create", "dry-created-0", "a"),
        ("create", "dry-created-0", "b"),
        ("create", "dry-created-1", TITLE_PROPERTY),
    ]
    # The record's own title doubles as the display title when no map is given.
    assert rows[0].entity_title == "Made Up"


# --- records_to_suggestions: whole-entity ops --------------------------------


def test_delete_publish_unpublish_produce_one_row_each() -> None:
    records = [
        _rec(op="delete", shared_id="s1"),
        _rec(op="publish", shared_id="s2"),
        _rec(op="unpublish", shared_id="s3"),
        _rec(op="set_publish_status", shared_id="s4", published=False),
    ]
    rows = records_to_suggestions("run1", 3, records)
    assert [(r.op, r.shared_id, r.property_name) for r in rows] == [
        ("delete", "s1", None),
        ("publish", "s2", None),
        ("unpublish", "s3", None),
        ("set_publish_status", "s4", None),
    ]
    # set_publish_status carries its target state so the UI can label it.
    assert rows[3].proposed_value is False
    # Whole-entity ops carry no match verdict.
    assert all(r.match is None for r in rows)


def test_create_relationships_produces_one_row() -> None:
    records = [_rec(op="create_relationships", from_entity_shared_id="a", to_entity_shared_id="b")]
    rows = records_to_suggestions("run1", 1, records)
    assert len(rows) == 1
    assert rows[0].op == "create_relationships"
    assert rows[0].property_name is None


def test_non_suggestion_ops_are_skipped() -> None:
    records = [
        _rec(op="move_files", from_shared_ids=["a"], to_shared_id="b"),
        _rec(op="delete_file", shared_id="s1", file_id="f1"),
        _rec(op="delete", shared_id="s2"),
    ]
    rows = records_to_suggestions("run1", 1, records)
    assert [(r.op, r.shared_id) for r in rows] == [("delete", "s2")]


def test_entity_titles_map_wins_over_record_title() -> None:
    records = [_rec(op="update", shared_id="s1", metadata={"a": 1}, title=None)]
    rows = records_to_suggestions("run1", 1, records, entity_titles={"s1": "Resolved Title"})
    assert rows[0].entity_title == "Resolved Title"


def test_records_to_suggestions_on_empty_input() -> None:
    assert records_to_suggestions("run1", 1, []) == []


# --- compare_values ----------------------------------------------------------


def test_compare_values_none_current_is_a_new_value() -> None:
    assert compare_values("anything", None) is None


def test_compare_values_display_equality() -> None:
    assert compare_values(["a"], ["a"]) is True
    assert compare_values([{"value": "x"}], [{"value": "y"}]) is False
    # A list and its rendered text compare equal — the display-equality rule.
    assert compare_values([{"label": "Topic A", "value": "topic_a"}], ["Topic A"]) is True


# --- score_accuracy ----------------------------------------------------------


def _suggestion(property_name: str | None, match: bool | None) -> Suggestion:
    return Suggestion(
        run_id="run1",
        attempt=1,
        shared_id="s1",
        op="update",
        property_name=property_name,
        match=match,
    )


def test_score_accuracy_counts_and_ratio() -> None:
    summary = score_accuracy(
        [
            _suggestion("excerpt", True),
            _suggestion("excerpt", True),
            _suggestion("excerpt", False),
            _suggestion("topic", True),
            _suggestion("topic", None),
            _suggestion(None, None),
        ]
    )
    assert (summary.matched, summary.mismatched, summary.new) == (3, 1, 1)
    assert summary.accuracy == 0.75
    assert summary.by_property["excerpt"].matched == 2
    assert summary.by_property["excerpt"].mismatched == 1
    assert summary.by_property["topic"].matched == 1
    assert summary.by_property["topic"].new == 1
    # Whole-entity rows never appear in the per-property breakdown.
    assert set(summary.by_property) == {"excerpt", "topic"}


def test_score_accuracy_none_when_nothing_comparable() -> None:
    assert score_accuracy([]).accuracy is None
    assert score_accuracy([_suggestion("a", None)]).accuracy is None
