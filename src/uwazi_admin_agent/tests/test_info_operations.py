"""Isolated unit tests for the display-only ``info`` operation (pure)."""

from __future__ import annotations

from uwazi_admin_agent.domain.suggestion import (
    INFO_OP,
    OperationReviewRow,
    Suggestion,
    SuggestionStatus,
)
from uwazi_admin_agent.domain.suggestion_scoring import records_to_suggestions
from uwazi_admin_agent.drivers.web import operations_view as view
from uwazi_admin_agent.use_cases.apply_suggestions_use_case import pushable_rows
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


def test_records_to_suggestions_expands_info_into_two_levels() -> None:
    rows = records_to_suggestions(
        "run1",
        1,
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
    rows = records_to_suggestions("run1", 1, [{"op": INFO_OP, "title": "Headline"}])

    assert len(rows) == 1
    assert rows[0].shared_id == "info-0"
    assert rows[0].proposed_value == "Headline"


def _info_operation() -> OperationReviewRow:
    parent = Suggestion(
        id=1,
        run_id="run1",
        attempt=1,
        shared_id="info-0",
        entity_title="A result headline",
        op=INFO_OP,
        proposed_value="A result headline",
    )
    child = Suggestion(
        id=2,
        run_id="run1",
        attempt=1,
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
        accepted=0,
        applied=0,
        reverted=0,
        discarded=0,
        edited=0,
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
        attempt=1,
        shared_id="info-0",
        op=INFO_OP,
        proposed_value="display only",
        status=SuggestionStatus.ACCEPTED,
    )
    update = Suggestion(
        id=2,
        run_id="run1",
        attempt=1,
        shared_id="entity-a",
        op="update",
        property_name="title",
        proposed_value="new",
        status=SuggestionStatus.ACCEPTED,
    )

    assert pushable_rows([info, update]) == [update]
