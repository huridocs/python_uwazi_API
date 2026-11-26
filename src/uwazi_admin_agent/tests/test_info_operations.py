"""Isolated unit tests for the display-only ``info`` operation (pure)."""

from __future__ import annotations

from uwazi_admin_agent.domain.suggestion import (
    INFO_OP,
    MATCHED_OP,
    SKIPPED_OP,
    OperationReviewRow,
    Suggestion,
    SuggestionStatus,
    skip_reason_label,
)
from uwazi_admin_agent.domain.suggestion_scoring import drop_unchanged_property_rows, records_to_suggestions
from uwazi_admin_agent.drivers.web import operations_view as view
from uwazi_admin_agent.use_cases.apply_suggestions_use_case import partition_for_apply, pushable_rows
from uwazi_admin_agent.use_cases.script_exec_namespace import _dry_run_write_helpers


def test_add_info_records_a_parent_and_its_children() -> None:
    records: list[dict] = []
    helpers = _dry_run_write_helpers(records)

    result = helpers["add_info"]("12 entities match", ["Alpha", "Beta"])

    assert result["success"] is True
    assert result["shared_id"] == "info-0"
    assert records == [
        {
            "op": INFO_OP,
            "shared_id": "info-0",
            "title": "12 entities match",
            "children": ["Alpha", "Beta"],
        }
    ]


def test_add_info_accepts_one_child_and_no_children() -> None:
    records: list[dict] = []
    add_info = _dry_run_write_helpers(records)["add_info"]

    add_info("one detail", "Only")
    add_info("no details")

    assert records[0]["children"] == ["Only"]
    assert records[1]["children"] == []
    assert records[0]["shared_id"] != records[1]["shared_id"]


def test_add_info_marks_a_row_to_keep_beside_changes() -> None:
    records: list[dict] = []
    add_info = _dry_run_write_helpers(records)["add_info"]

    add_info("2 entities failed", ["S9: timeout"], keep=True)

    assert records == [
        {
            "op": INFO_OP,
            "shared_id": "info-0",
            "title": "2 entities failed",
            "children": ["S9: timeout"],
            "keep": True,
        }
    ]


def test_info_only_plan_keeps_its_answer() -> None:
    rows = records_to_suggestions(
        "run1",
        [{"op": INFO_OP, "shared_id": "info-0", "title": "The answer", "children": []}],
    )

    assert [(row.op, row.proposed_value) for row in rows] == [(INFO_OP, "The answer")]


def test_plain_info_is_dropped_when_the_same_plan_records_a_change() -> None:
    """A change plan explains itself: a summary info row is noise.

    This is the regression guard for a "moved N files" info row appearing below
    the move operation: the operation already shows the files and the target.
    """
    rows = records_to_suggestions(
        "run1",
        [
            {
                "op": "move_files",
                "shared_id": "T",
                "to_shared_id": "T",
                "from_shared_ids": ["S1"],
            },
            {
                "op": INFO_OP,
                "shared_id": "info-0",
                "title": "Appended 2 files to T",
                "children": ["one.pdf", "two.pdf"],
            },
        ],
    )

    assert [(row.op, row.shared_id) for row in rows] == [("move_files", "T")]


def test_a_kept_info_row_survives_beside_changes() -> None:
    rows = records_to_suggestions(
        "run1",
        [
            {"op": "update", "shared_id": "T", "metadata": {"a": "new"}},
            {
                "op": INFO_OP,
                "shared_id": "info-0",
                "title": "1 entity failed",
                "children": ["S9: timeout"],
                "keep": True,
            },
        ],
    )

    assert [row.op for row in rows] == ["update", INFO_OP, INFO_OP]
    assert [row.proposed_value for row in rows[1:]] == ["1 entity failed", "S9: timeout"]


def test_records_to_suggestions_expands_info_into_two_levels() -> None:
    rows = records_to_suggestions(
        "run1",
        [
            {
                "op": INFO_OP,
                "shared_id": "info-3",
                "title": "Query answer",
                "children": ["first", 2],
            }
        ],
    )

    assert [row.op for row in rows] == [INFO_OP, INFO_OP, INFO_OP]
    assert {row.shared_id for row in rows} == {"info-3"}
    assert {row.entity_title for row in rows} == {"Query answer"}

    parent = rows[0]
    assert parent.proposed_value == "Query answer"
    assert parent.detail_key is None

    first, second = rows[1], rows[2]
    assert first.proposed_value == "first"
    assert first.detail_key == "info-child-0"
    assert second.proposed_value == "2"
    assert second.detail_key == "info-child-1"


def test_records_to_suggestions_invents_an_info_id_when_missing() -> None:
    rows = records_to_suggestions("run1", [{"op": INFO_OP, "title": "Headline"}])

    assert len(rows) == 1
    assert rows[0].shared_id == "info-0"
    assert rows[0].proposed_value == "Headline"


def _info_operation() -> OperationReviewRow:
    parent = Suggestion(
        id=1,
        run_id="run1",
        shared_id="info-0",
        entity_title="A result headline",
        op=INFO_OP,
        proposed_value="A result headline",
    )
    child = Suggestion(
        id=2,
        run_id="run1",
        shared_id="info-0",
        entity_title="A result headline",
        op=INFO_OP,
        proposed_value="A detail line",
        detail_key="info-child-0",
    )
    return OperationReviewRow(
        shared_id="info-0",
        entity_title="A result headline",
        template_names=[],
        ops=[INFO_OP],
        properties=[],
        languages=[],
        proposal_count=2,
        pending=2,
        applied=0,
        reverted=0,
        discarded=0,
        match_summary="n/a",
        has_outcome=False,
        subs=[parent, child],
    )


def test_operation_row_renders_info_without_actions_or_selection() -> None:
    payload = view.operation_row(_info_operation(), uwazi_url="https://example.test")

    assert payload["is_info"] is True
    assert payload["title"] == "A result headline"
    assert payload["title_full"] == "A result headline"
    assert payload["run_status"] == "info"
    assert payload["url"] is None
    assert payload["has_checkable"] is False
    assert payload["has_validated"] is False
    assert payload["revertable"] is False
    assert payload["has_subs"] is True

    # The parent is not a suboperation: only the detail line is rendered.
    assert [sub["label"] for sub in payload["subs"]] == ["A detail line"]
    assert payload["subs"][0]["is_info"] is True
    assert payload["subs"][0]["selectable"] is False
    assert payload["subs"][0]["checked"] is False


def test_operation_row_hides_the_expander_when_info_has_no_children() -> None:
    item = _info_operation().model_copy(update={"subs": _info_operation().subs[:1]})

    payload = view.operation_row(item)

    assert payload["is_info"] is True
    assert payload["subs"] == []
    assert payload["has_subs"] is False


def test_pushable_rows_never_applies_info() -> None:
    info = Suggestion(
        id=1,
        run_id="run1",
        shared_id="info-0",
        op=INFO_OP,
        proposed_value="display only",
        status=SuggestionStatus.PENDING,
    )
    update = Suggestion(
        id=2,
        run_id="run1",
        shared_id="entity-a",
        op="update",
        property_name="title",
        proposed_value="new",
        status=SuggestionStatus.PENDING,
    )

    assert pushable_rows([info, update]) == [update]


# --- matched / skipped display-only outcomes ---------------------------------


def _display_operation(op: str, *, reason: str | None = None) -> OperationReviewRow:
    sub = Suggestion(
        id=20,
        run_id="run1",
        shared_id="entity-a",
        entity_title="A/HRC/37/2",
        template_name="DOCUMENT",
        op=op,  # type: ignore[arg-type]
        language=None if op == SKIPPED_OP else "en",
        property_name=None if op == SKIPPED_OP else "document_ref",
        current_value=None if op == SKIPPED_OP else "A/HRC/37/2",
        proposed_value="first page excerpt" if op == SKIPPED_OP else "A/HRC/37/2",
        skip_reason=reason,
        match=True if op == MATCHED_OP else None,
        status=SuggestionStatus.PENDING,
    )
    return OperationReviewRow(
        shared_id="entity-a",
        entity_title="A/HRC/37/2",
        template_names=["DOCUMENT"],
        ops=[op],
        properties=[] if op == SKIPPED_OP else ["document_ref"],
        languages=[] if op == SKIPPED_OP else ["en"],
        proposal_count=1,
        pending=1,
        applied=0,
        reverted=0,
        discarded=0,
        match_summary="n/a",
        has_outcome=False,
        subs=[sub],
    )


def test_skipped_reason_ids_map_to_descriptive_text() -> None:
    assert skip_reason_label("no_segmentation") == "The primary document has no usable text segmentation."
    assert skip_reason_label("no_document") == "No primary document is attached to this entity."
    assert skip_reason_label("no_code_in_page_1") == "No matching value was found in the source."
    assert skip_reason_label("no_match") == "No matching value was found in the source."
    assert skip_reason_label(None).startswith("This entity was excluded")


def test_skip_record_becomes_a_per_entity_display_row() -> None:
    records: list[dict] = []
    record_skipped = _dry_run_write_helpers(records)["record_skipped"]

    record_skipped("entity-a", "no_segmentation", title="A/HRC/37/2", template_name="DOCUMENT", detail="page 1")

    assert records[0]["op"] == SKIPPED_OP
    assert records[0]["shared_id"] == "entity-a"
    rows = records_to_suggestions("run1", records)
    assert len(rows) == 1
    assert rows[0].shared_id == "entity-a"
    assert rows[0].entity_title == "A/HRC/37/2"
    assert rows[0].template_name == "DOCUMENT"
    assert rows[0].skip_reason == "no_segmentation"
    assert rows[0].proposed_value == "page 1"


def test_matched_operation_has_no_actions_but_has_link_and_values() -> None:
    payload = view.operation_row(_display_operation(MATCHED_OP), uwazi_url="https://example.test")

    assert payload["url"] == "https://example.test/en/entity/entity-a"
    assert payload["has_checkable"] is False
    assert payload["has_runnable"] is False
    assert payload["revertable"] is False
    assert payload["is_display_only"] is True
    assert payload["run_status"] == "matched"
    sub = payload["subs"][0]
    assert (sub["current"], sub["proposed"]) == ("A/HRC/37/2", "A/HRC/37/2")
    assert sub["status"] == "matched"
    assert sub["selectable"] is False


def test_skipped_operation_shows_the_reason_and_entity_link() -> None:
    payload = view.operation_row(_display_operation(SKIPPED_OP, reason="no_segmentation"), uwazi_url="https://example.test")

    assert payload["url"] == "https://example.test/en/entity/entity-a"
    assert payload["run_status"] == "excluded"
    assert payload["is_display_only"] is True
    assert payload["has_runnable"] is False
    sub = payload["subs"][0]
    assert sub["label"] == "The primary document has no usable text segmentation."
    assert sub["caption"] == ""
    # The exclusion's evidence is the INPUT text, never a suggested value.
    assert sub["proposed"] == "—"
    assert sub["context"] == "first page excerpt"
    assert sub["status"] == "excluded"
    assert sub["selectable"] is False


def test_matched_unchanged_outputs_become_matched_rows() -> None:
    row = Suggestion(
        id=1,
        run_id="run1",
        shared_id="entity-a",
        op="update",
        property_name="document_ref",
        language="en",
        current_value="A/HRC/37/2",
        proposed_value="A/HRC/37/2",
        match=True,
        status=SuggestionStatus.PENDING,
    )

    kept = drop_unchanged_property_rows([row])

    assert len(kept) == 1
    assert kept[0].op == MATCHED_OP
    assert kept[0].current_value == "A/HRC/37/2"
    assert kept[0].proposed_value == "A/HRC/37/2"
    assert kept[0].match is True


def test_pushable_rows_and_apply_partitions_exclude_matched_and_skipped() -> None:
    matched = Suggestion(
        id=1,
        run_id="run1",
        shared_id="a",
        op=MATCHED_OP,
        property_name="document_ref",
        proposed_value="same",
        status=SuggestionStatus.PENDING,
    )
    skipped = Suggestion(
        id=2,
        run_id="run1",
        shared_id="b",
        op=SKIPPED_OP,
        status=SuggestionStatus.PENDING,
    )

    assert pushable_rows([matched, skipped]) == []
    assert partition_for_apply([matched, skipped]) == ([], [])


def test_update_records_the_context_it_is_given() -> None:
    records: list[dict] = []
    update_entities = _dry_run_write_helpers(records)["update_entities"]

    update_entities([{"shared_id": "s1", "metadata": {"topic": ["T"]}, "context": "a paragraph"}])

    assert records[0]["context"] == "a paragraph"


def test_update_records_no_context_when_there_is_none() -> None:
    records: list[dict] = []
    update_entities = _dry_run_write_helpers(records)["update_entities"]

    update_entities([{"shared_id": "s1", "metadata": {"topic": ["T"]}}])

    assert records[0]["context"] is None


def test_a_reserved_context_key_inside_metadata_is_promoted_and_dropped() -> None:
    """An extractor may return the passage under ``_context``; it must never
    reach Uwazi as a property and must still show as the row's evidence."""
    records: list[dict] = []
    update_entities = _dry_run_write_helpers(records)["update_entities"]

    update_entities([{"shared_id": "s1", "metadata": {"topic": ["T"], "_context": "the exact passage"}, "context": None}])

    assert records[0]["metadata"] == {"topic": ["T"]}
    assert records[0]["context"] == "the exact passage"


def test_an_explicit_context_wins_over_the_metadata_key() -> None:
    records: list[dict] = []
    update_entities = _dry_run_write_helpers(records)["update_entities"]

    update_entities(
        [
            {
                "shared_id": "s1",
                "metadata": {"topic": ["T"], "_context": "from metadata"},
                "context": "explicit passage",
            }
        ]
    )

    assert records[0]["metadata"] == {"topic": ["T"]}
    assert records[0]["context"] == "explicit passage"


def test_a_list_of_paragraphs_is_joined_into_one_context() -> None:
    records: list[dict] = []
    update_entities = _dry_run_write_helpers(records)["update_entities"]

    update_entities([{"shared_id": "s1", "metadata": {"topic": ["T"]}, "context": ["first", "  second  "]}])

    assert records[0]["context"] == "first\n\nsecond"
