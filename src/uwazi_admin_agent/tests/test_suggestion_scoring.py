"""Isolated unit tests for record→suggestion conversion + accuracy scoring (per AGENTS.md).

Pure: literal dry-run record dicts in, real :class:`Suggestion` rows out. No
mocks, no I/O, no instance.
"""

from uwazi_admin_agent.domain.suggestion import TITLE_PROPERTY, Suggestion, SuggestionStatus
from uwazi_admin_agent.domain.suggestion_scoring import (
    compare_values,
    records_to_suggestions,
    row_language,
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
    rows = records_to_suggestions("run1", records)
    assert [(r.op, r.property_name, r.shared_id) for r in rows] == [
        ("update", "excerpt", "s1"),
        ("update", "topic", "s1"),
    ]
    assert rows[0].proposed_value == [{"value": "v1"}]
    assert all(r.status == SuggestionStatus.PENDING for r in rows)
    assert all(r.run_id == "run1" for r in rows)
    assert all(r.created_at is not None for r in rows)


def test_update_record_with_title_emits_title_row_first() -> None:
    records = [_rec(op="update", shared_id="s1", metadata={"a": 1}, title="New Title")]
    rows = records_to_suggestions("run1", records)
    assert [(r.property_name, r.proposed_value) for r in rows] == [
        (TITLE_PROPERTY, "New Title"),
        ("a", 1),
    ]


def test_update_record_with_no_metadata_and_no_title_emits_a_single_placeholder_row() -> None:
    records = [_rec(op="update", shared_id="s1", metadata={}, title=None)]
    rows = records_to_suggestions("run1", records)
    assert len(rows) == 1
    assert rows[0].property_name is None
    assert rows[0].proposed_value is None


def test_update_record_with_missing_metadata_key_still_emits_placeholder() -> None:
    rows = records_to_suggestions("run1", [_rec(op="update", shared_id="s1")])
    assert [r.property_name for r in rows] == [None]


# --- records_to_suggestions: the record's language -----------------------------


def test_a_scalar_value_files_under_the_language_the_script_passed() -> None:
    """A bare-scalar per-language value takes the script's ``language`` argument.

    The script contract writes ``text``/``markdown`` as a scalar and states the
    target language in the write helper's argument
    (``update_entities(updates, language="es")``), so that argument is the ONLY
    carrier of the intent. Dropping it filed the Spanish row under the
    language-agnostic "—" group of the review table.
    """
    records = [
        _rec(
            op="update",
            shared_id="elfd96ccepk",
            template_name="DOCUMENT",
            metadata={"document_ref": "A/HRC/37/NGO/92 ES"},
            title=None,
            language="es",
        )
    ]
    rows = records_to_suggestions("update", records)
    assert [(r.shared_id, r.language, r.proposed_value) for r in rows] == [("elfd96ccepk", "es", "A/HRC/37/NGO/92 ES")]


def test_a_value_naming_its_own_languages_still_splits_per_language() -> None:
    """The value's own languages win: the fallback only fills a gap."""
    records = [
        _rec(
            op="update",
            shared_id="s1",
            template_name="DOCUMENT",
            language="es",
            metadata={"document_ref": [{"value": "hola", "language": "es"}, {"value": "hello", "language": "en"}]},
        )
    ]
    rows = records_to_suggestions("run1", records)
    assert [(r.language, r.proposed_value) for r in rows] == [
        ("es", [{"value": "hola", "language": "es"}]),
        ("en", [{"value": "hello", "language": "en"}]),
    ]


def test_a_record_without_a_language_files_under_none() -> None:
    rows = records_to_suggestions("run1", [_rec(op="update", shared_id="s1", metadata={"document_ref": "x"})])
    assert rows[0].language is None


def test_a_blank_or_non_string_record_language_files_under_none() -> None:
    for bogus in ("", None, 7, []):
        rows = records_to_suggestions("run1", [_rec(op="update", shared_id="s1", metadata={"a": 1}, language=bogus)])
        assert rows[0].language is None, bogus


def test_whole_entity_rows_stay_language_agnostic() -> None:
    """A delete carries no property value, so the write's language says nothing about it."""
    rows = records_to_suggestions("run1", [_rec(op="delete", shared_id="s1", language="es")])
    assert rows[0].language is None


def test_row_language_is_pure() -> None:
    assert row_language("es", "en") == "es"  # the value wins
    assert row_language(None, "es") == "es"  # the record fills the gap
    assert row_language(None, None) is None
    assert row_language("", "") is None
    assert row_language(None, 7) is None


# --- records_to_suggestions: the property's language scope ---------------------


def test_a_multiselect_change_is_one_suboperation_with_no_language() -> None:
    """A shared value is ONE change: it is not filed under a language.

    A multiselect holds one value shared by every language row of the entity,
    so the write helper's ``language`` argument says nothing about the value.
    Filing it under that language would offer the operator a per-language row
    for a change that has no language.
    """
    records = [
        _rec(
            op="update",
            shared_id="lii2m1m62f9",
            template_name="Case",
            metadata={"languages": ["English", "Spanish"]},
            title=None,
            language="en",
        )
    ]
    rows = records_to_suggestions("run1", records, property_types={"languages": "multiselect"})
    assert [(r.property_name, r.language) for r in rows] == [("languages", None)]


def test_a_multiselect_written_under_two_languages_is_still_one_suboperation() -> None:
    """The same shared value written once per language is ONE change.

    The script may well write the shared value in several language rows (the
    helper's ``language`` argument selects the ROW, not the value). That is
    still one change to one shared value, so it must not become two rows the
    operator could run separately — the second would overwrite the first with
    the same value.
    """
    records = [
        _rec(
            op="update",
            shared_id="lii2m1m62f9",
            template_name="Case",
            metadata={"languages": ["English", "Spanish"]},
            title=None,
            language=language,
        )
        for language in ("en", "es")
    ]
    rows = records_to_suggestions("run1", records, property_types={"languages": "multiselect"})
    assert [(r.property_name, r.language) for r in rows] == [("languages", None)]


def test_a_text_property_still_splits_per_language() -> None:
    """The free-text types are stored per language row, so they still split."""
    records = [
        _rec(
            op="update",
            shared_id="s1",
            template_name="Case",
            metadata={"summary": [{"value": "hello", "language": "en"}, {"value": "hola", "language": "es"}]},
            title=None,
            language="en",
        )
    ]
    rows = records_to_suggestions("run1", records, property_types={"summary": "text"})
    assert [r.language for r in rows] == ["en", "es"]


def test_a_text_property_written_as_bare_scalars_still_splits_per_language() -> None:
    """The contract writes text as a scalar and states the language in the helper."""
    records = [
        _rec(
            op="update",
            shared_id="s1",
            template_name="Case",
            metadata={"summary": value},
            title=None,
            language=language,
        )
        for language, value in (("en", "hello"), ("es", "hola"))
    ]
    rows = records_to_suggestions("run1", records, property_types={"summary": "text"})
    assert [(r.language, r.proposed_value) for r in rows] == [("en", "hello"), ("es", "hola")]


def test_without_the_types_the_row_keeps_the_scripts_language() -> None:
    """With no template definitions at all, the script's language still carries the intent.

    The contract writes a text value as a bare scalar too, so a bare value is
    ambiguous without the types — and the recording path keeps the behaviour it
    has always had rather than guessing.
    """
    records = [
        _rec(
            op="update",
            shared_id="lii2m1m62f9",
            template_name="Case",
            metadata={"languages": ["English", "Spanish"]},
            title=None,
            language="en",
        )
    ]
    rows = records_to_suggestions("run1", records)
    assert [(r.property_name, r.language) for r in rows] == [("languages", "en")]


def test_a_value_naming_its_own_languages_is_per_language_without_the_types() -> None:
    """A value that names its languages is per-language whatever the types say."""
    records = [
        _rec(
            op="update",
            shared_id="s1",
            template_name="Case",
            metadata={"summary": [{"value": "hello", "language": "en"}]},
            title=None,
            language="en",
        )
    ]
    rows = records_to_suggestions("run1", records)
    assert [(r.property_name, r.language) for r in rows] == [("summary", "en")]


def test_a_shared_value_of_one_entity_does_not_swallow_another_entitys() -> None:
    """The collapse is per entity: two entities keep their own row."""
    records = [
        _rec(
            op="update",
            shared_id=shared_id,
            template_name="Case",
            metadata={"languages": ["English"]},
            title=None,
            language="en",
        )
        for shared_id in ("s1", "s2")
    ]
    rows = records_to_suggestions("run1", records, property_types={"languages": "multiselect"})
    assert [(r.shared_id, r.language) for r in rows] == [("s1", None), ("s2", None)]


# --- records_to_suggestions: create ------------------------------------------


def test_create_record_uses_placeholder_id_and_explodes_metadata() -> None:
    records = [
        _rec(op="create", title="Made Up", template_name="Case", metadata={"a": 1, "b": 2}),
        _rec(op="create", title="Second", template_name="Case", metadata={}),
    ]
    rows = records_to_suggestions("run1", records)
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
    rows = records_to_suggestions("run1", records)
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


def test_create_relationships_produces_one_row_per_link() -> None:
    records = [
        _rec(op="create_relationships", from_entity_shared_id="a", to_entity_shared_id="b", relationship_type="ref"),
        _rec(op="create_relationships", from_entity_shared_id="a", to_entity_shared_id="c", relationship_type="ref"),
    ]
    rows = records_to_suggestions("run1", records)
    assert len(rows) == 2
    assert all(row.op == "create_relationships" for row in rows)
    assert all(row.property_name is None for row in rows)
    # The entity the batch belongs to is the FROM side, and each link's target
    # rides on detail_key so the rows stay distinct in the store's identity.
    assert [row.shared_id for row in rows] == ["a", "a"]
    assert [row.detail_key for row in rows] == ["b", "c"]
    # The apply path reads the link back from proposed_value, so the record's
    # payload must survive verbatim.
    assert rows[0].proposed_value["to_entity_shared_id"] == "b"
    assert rows[0].proposed_value["relationship_type"] == "ref"
    assert "op" not in rows[0].proposed_value


def test_non_suggestion_ops_are_skipped() -> None:
    records = [
        _rec(op="move_files", from_shared_ids=["a"], to_shared_id="b"),
        _rec(op="delete_file", shared_id="s1", file_id="f1"),
        _rec(op="delete", shared_id="s2"),
    ]
    rows = records_to_suggestions("run1", records)
    assert [(r.op, r.shared_id) for r in rows] == [("delete", "s2")]


def test_entity_titles_map_wins_over_record_title() -> None:
    records = [_rec(op="update", shared_id="s1", metadata={"a": 1}, title=None)]
    rows = records_to_suggestions("run1", records, entity_titles={"s1": "Resolved Title"})
    assert rows[0].entity_title == "Resolved Title"


def test_records_to_suggestions_on_empty_input() -> None:
    assert records_to_suggestions("run1", []) == []


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
