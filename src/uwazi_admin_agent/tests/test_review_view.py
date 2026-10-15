"""Isolated unit tests for the review UI's pure display + filter helpers (per AGENTS.md).

No NiceGUI import, no I/O: the helpers are pure transforms of domain objects and
control values, so they are testable directly.
"""

from uwazi_admin_agent.domain.suggestion import EntityReviewRow, Suggestion, SuggestionStatus
from uwazi_admin_agent.drivers.web.review_view import (
    accuracy_label,
    entity_row,
    filter_from_controls,
    match_label,
    run_confirmation,
    run_status,
    suggestion_to_row,
    truncate,
)


def _suggestion(**kwargs: object) -> Suggestion:
    base: dict[str, object] = {
        "id": 7,
        "run_id": "r",
        "attempt": 1,
        "shared_id": "s1",
        "op": "update",
        "property_name": "excerpt",
        "proposed_value": [{"value": "new"}],
        "current_value": [{"value": "old"}],
        "match": False,
    }
    base.update(kwargs)
    return Suggestion(**base)  # type: ignore[arg-type]


# --- truncate ---------------------------------------------------------------


def test_truncate_renders_metadata_shapes() -> None:
    assert truncate([{"value": "a"}, {"value": "b"}]) == "a, b"
    assert truncate([{"label": "Topic A", "value": "x"}]) == "Topic A"
    assert truncate(None) == "—"
    assert truncate(True) == "true"


def test_truncate_appends_an_ellipsis_past_the_limit() -> None:
    assert truncate("x" * 200, limit=10) == "x" * 10 + "…"
    assert truncate("short", limit=10) == "short"


# --- match_label ------------------------------------------------------------


def test_match_label_maps_the_tri_state() -> None:
    assert match_label(True) == "exact"
    assert match_label(False) == "mismatch"
    assert match_label(None) == "new"


# --- suggestion_to_row ------------------------------------------------------


def test_suggestion_to_row_flattens_the_display_contract() -> None:
    row = suggestion_to_row(_suggestion(entity_title="Title", status=SuggestionStatus.ACCEPTED))
    assert row["id"] == 7
    assert row["shared_id"] == "s1"
    assert row["title"] == "Title"
    assert row["op"] == "update"
    assert row["property"] == "excerpt"
    assert row["proposed"] == "new"
    assert row["current"] == "old"
    assert row["match"] == "mismatch"
    assert row["status"] == "accepted"
    assert row["editable"] is True


def test_suggestion_to_row_carries_the_badge_colors() -> None:
    """Vue slots cannot reach ``window.*``, so colors must ride on the row."""
    row = suggestion_to_row(_suggestion(op="delete", property_name=None, match=None, status=SuggestionStatus.DISCARDED))
    assert row["op_color"] == "negative"
    assert row["match_color"] == "grey-6"
    assert row["status_color"] == "red"


def test_suggestion_to_row_uses_shared_id_when_there_is_no_title() -> None:
    assert suggestion_to_row(_suggestion(entity_title=""))["title"] == "s1"


def test_suggestion_to_row_dashes_a_whole_entity_op_and_marks_it_ineditable() -> None:
    row = suggestion_to_row(_suggestion(op="delete", property_name=None, match=None))
    assert row["property"] == "—"
    assert row["match"] == "new"
    assert row["editable"] is False


# --- filter_from_controls ---------------------------------------------------


def test_filter_from_controls_maps_empty_strings_to_no_constraint() -> None:
    f = filter_from_controls()
    assert f.status is None
    assert f.op is None
    assert f.property_name is None
    assert f.match is None
    assert f.text is None
    assert f.accepted_only is False


def test_filter_from_controls_maps_values_through() -> None:
    f = filter_from_controls(status="accepted", op="update", property_name="excerpt", match="mismatch", text=" foo ")
    assert f.status == SuggestionStatus.ACCEPTED
    assert f.op == "update"
    assert f.property_name == "excerpt"
    assert f.match == "mismatch"
    assert f.text == "foo"  # trimmed


def test_filter_from_controls_whitespace_text_is_no_constraint() -> None:
    assert filter_from_controls(text="   ").text is None


def test_filter_from_controls_keeps_paging_and_the_accepted_flag() -> None:
    f = filter_from_controls(offset=50, limit=25, accepted_only=True)
    assert (f.offset, f.limit, f.accepted_only) == (50, 25, True)


# --- accuracy_label ---------------------------------------------------------


def test_accuracy_label_computes_a_percentage() -> None:
    assert accuracy_label(3, 1) == "75.0%"
    assert accuracy_label(0, 1) == "0.0%"
    assert accuracy_label(1, 0) == "100.0%"


def test_accuracy_label_is_na_when_nothing_compares() -> None:
    assert accuracy_label(0, 0) == "n/a"


# --- entity_row ----------------------------------------------------------------


def _entity_row(
    *,
    shared_id: str = "s1",
    entity_title: str = "One",
    template_names: list[str] | None = None,
    ops: list[str] | None = None,
    properties: list[str] | None = None,
    proposal_count: int = 2,
    pending: int = 2,
    accepted: int = 0,
    applied: int = 0,
    reverted: int = 0,
    discarded: int = 0,
    match_summary: str = "exact",
    has_outcome: bool = False,
) -> EntityReviewRow:
    return EntityReviewRow(
        shared_id=shared_id,
        entity_title=entity_title,
        template_names=template_names if template_names is not None else ["Judgment"],
        ops=ops if ops is not None else ["update"],
        properties=properties if properties is not None else ["excerpt"],
        proposal_count=proposal_count,
        pending=pending,
        accepted=accepted,
        applied=applied,
        reverted=reverted,
        discarded=discarded,
        match_summary=match_summary,
        has_outcome=has_outcome,
    )


def test_entity_row_flattens_the_entity_contract() -> None:
    row = entity_row(_entity_row(template_names=["Judgment"]))
    assert row["shared_id"] == "s1"
    assert row["title"] == "One"
    assert row["ops"] == "update"
    assert row["op_colors"] == ["primary"]
    assert row["template"] == "Judgment"


def test_entity_row_uses_shared_id_when_there_is_no_title() -> None:
    assert entity_row(_entity_row(entity_title=""))["title"] == "s1"


def test_entity_row_dashes_an_unknown_template() -> None:
    row = entity_row(_entity_row(template_names=[], ops=["delete"]))
    assert row["template"] == "—"
    assert row["op_colors"] == ["negative"]


def test_entity_row_mixed_ops_keep_their_colors_aligned() -> None:
    row = entity_row(_entity_row(ops=["update", "delete"]))
    assert row["ops"] == "update, delete"
    assert row["op_colors"] == ["primary", "negative"]


def test_entity_row_match_column_is_gone() -> None:
    row = entity_row(_entity_row())
    assert "match" not in row
    assert "match_color" not in row
    assert "properties" not in row
    assert "proposals" not in row
    assert "status_summary" not in row


# --- run status --------------------------------------------------------------


def test_run_status_is_pending_for_an_unreviewed_entity() -> None:
    assert run_status(_entity_row(pending=2, applied=0)) == ("pending", "grey")


def test_run_status_is_runned_once_every_row_is_applied() -> None:
    assert run_status(_entity_row(pending=0, applied=2)) == ("runned", "blue")


def test_run_status_is_reverted_once_a_revert_undid_the_writes() -> None:
    assert run_status(_entity_row(pending=0, applied=0, reverted=2)) == ("reverted", "indigo")


def test_run_status_stays_pending_on_a_partial_state() -> None:
    # One applied + one reverted: work remains, and it is not fully runned.
    assert run_status(_entity_row(pending=0, applied=1, reverted=1)) == ("pending", "grey")


def test_filter_from_controls_passes_shared_ids_through() -> None:
    f = filter_from_controls(shared_ids=["a", "b"])
    assert f.shared_ids == ["a", "b"]
    assert filter_from_controls().shared_ids is None
