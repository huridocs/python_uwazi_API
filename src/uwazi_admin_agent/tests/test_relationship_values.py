"""Isolated unit tests for relationship values and the select/relationship pickers (pure)."""

from __future__ import annotations

from uwazi_admin_agent.domain.relationship_values import (
    PICKABLE_TYPES,
    relationship_options,
    relationship_selection,
    relationship_title,
    render_relationship,
    shared_ids_of,
    single_value,
)
from uwazi_admin_agent.domain.suggestion import Suggestion, SuggestionStatus
from uwazi_admin_agent.drivers.web import operations_view as view


def _sub(
    *,
    property_name: str = "related",
    proposed: object = [{"value": "6g14bm8ub1a"}],
    current: object = None,
    selected: list[str] | None = None,
    rejected: list[str] | None = None,
) -> Suggestion:
    return Suggestion(
        id=1,
        run_id="run-1",
        shared_id="entity-a",
        op="update",
        property_name=property_name,
        language=None,
        status=SuggestionStatus.PENDING,
        proposed_value=proposed,
        current_value=current,
        selected_values=selected,
        rejected_values=rejected or [],
    )


# --- shared id extraction -----------------------------------------------------


def test_shared_ids_reads_every_uwazi_shape() -> None:
    assert shared_ids_of([{"value": "a"}, {"value": "b"}]) == ["a", "b"]
    assert shared_ids_of([{"shared_id": "a", "title": "Alpha"}]) == ["a"]
    assert shared_ids_of([{"sharedId": "a"}]) == ["a"]
    assert shared_ids_of("a") == ["a"]
    assert shared_ids_of(["a", "b"]) == ["a", "b"]
    assert shared_ids_of(None) == []
    assert shared_ids_of("") == []


def test_shared_ids_collapse_duplicates_in_order() -> None:
    assert shared_ids_of([{"value": "b"}, {"value": "a"}, {"value": "b"}]) == ["b", "a"]


# --- rendering ----------------------------------------------------------------


def test_relationship_renders_titles_and_falls_back_to_the_id() -> None:
    titles = {"a": "Alpha Report"}
    assert render_relationship([{"value": "a"}, {"value": "zz"}], titles) == "Alpha Report, zz"
    assert render_relationship([], titles) == "—"
    assert relationship_title("a", titles) == "Alpha Report"
    assert relationship_title("missing", titles) == "missing"


def test_relationship_options_list_proposal_then_catalog_then_strays() -> None:
    options = relationship_options(
        [{"value": "a"}],
        [],
        ["c"],
        [("a", "Alpha"), ("b", "Beta"), ("c", "Gamma"), ("d", "Delta")],
        {"a": "Alpha"},
    )
    assert [option["id"] for option in options] == ["a", "b", "c", "d"]
    assert [option["recommended"] for option in options] == [True, False, False, False]
    # ``selected`` is the EXPLICIT selection, so only "c" is accepted.
    assert [option["selected"] for option in options] == [False, False, True, False]


def test_relationship_options_show_a_selected_id_the_catalog_omits() -> None:
    options = relationship_options(None, [], ["gone"], [], {"gone": "Removed Entity"})
    assert options == [
        {"id": "gone", "label": "Removed Entity", "recommended": False, "selected": True},
    ]


# --- selection / write semantics ---------------------------------------------


def test_relationship_selection_prefers_the_explicit_selection() -> None:
    proposal = [{"value": "a"}, {"value": "b"}]
    assert relationship_selection(proposal, [], None) == ["a", "b"]
    assert relationship_selection(proposal, ["b"], None) == ["a"]
    assert relationship_selection(proposal, [], ["a", "c"]) == ["a", "c"]
    assert relationship_selection(proposal, [], []) == []


def test_relationship_options_marks_a_dropped_proposal_as_unaccepted() -> None:
    options = relationship_options([{"value": "a"}, {"value": "b"}], ["b"], ["a", "c"], [])
    assert {option["id"]: option["selected"] for option in options} == {"a": True, "b": False, "c": True}


def test_single_value_collapses_a_select_but_keeps_a_multiselect() -> None:
    assert single_value("select", ["a", "b"]) == ["a"]
    assert single_value("multiselect", ["a", "b"]) == ["a", "b"]
    assert single_value("relationship", ["a", "b"]) == ["a", "b"]
    assert PICKABLE_TYPES == {"select", "multiselect", "relationship"}


# --- table payload ------------------------------------------------------------


def test_table_row_shows_relationship_target_titles_not_ids() -> None:
    sub = _sub(
        proposed=[{"value": "6g14bm8ub1a"}, {"value": "9fp3cucotxf"}],
        current=[{"value": "gbiopy7cgdc"}],
    )
    row = view.suboperation_row(
        sub,
        checked=True,
        in_place=True,
        types={"related": "relationship"},
        titles={"6g14bm8ub1a": "First Report", "9fp3cucotxf": "Second Report", "gbiopy7cgdc": "Old Report"},
    )
    assert row["select_type"] == "relationship"
    assert row["current"] == "Old Report"
    assert row["proposed"] == "First Report, Second Report"
    assert [chip["label"] for chip in row["values"]] == ["First Report", "Second Report"]
    assert row["selection_text"] == "First Report, Second Report"


def test_table_row_marks_a_dropped_relationship_target_and_an_added_one() -> None:
    sub = _sub(
        proposed=[{"value": "a"}, {"value": "b"}],
        selected=["a", "c"],
    )
    row = view.suboperation_row(
        sub,
        checked=True,
        in_place=True,
        types={"related": "relationship"},
        titles={"a": "Alpha", "b": "Beta", "c": "Gamma"},
    )
    assert row["values"] == [{"label": "Alpha", "accepted": True}, {"label": "Beta", "accepted": False}]
    assert row["has_rejected"] is True
    assert row["selection_text"] == "Alpha, Gamma"


def test_table_row_renders_a_select_as_value_chips() -> None:
    sub = _sub(property_name="topic", proposed=["Alpha"], selected=["Beta"])
    row = view.suboperation_row(sub, checked=True, in_place=True, types={"topic": "select"})
    assert row["select_type"] == "select"
    assert row["values"] == [{"label": "Alpha", "accepted": False}]
    assert row["selection_text"] == "Beta"


def test_table_row_leaves_a_text_row_untouched() -> None:
    sub = _sub(property_name="excerpt", proposed="some text", current="old text")
    row = view.suboperation_row(sub, checked=True, in_place=True, types={"excerpt": "text"})
    assert row["select_type"] is None
    assert row["values"] == []
    assert row["current"] == "old text"
