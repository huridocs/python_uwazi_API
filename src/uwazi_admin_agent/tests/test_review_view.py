"""Isolated unit tests for the review UI's pure display + filter helpers (per AGENTS.md).

No NiceGUI import, no I/O: the helpers are pure transforms of domain objects and
control values, so they are testable directly.
"""

from datetime import datetime, timezone

from uwazi_admin_agent.domain.suggestion import TITLE_PROPERTY, EntityReviewRow, Suggestion, SuggestionStatus
from uwazi_admin_agent.drivers.web.review_view import (
    COLUMN_BY_SORT_KEY,
    OPERATION_SORT_OPTIONS,
    SORT_KEY_BY_COLUMN,
    TITLE_CELL_CHARS,
    cost_card_value,
    entity_language_key,
    entity_open_url,
    entity_row,
    filter_from_controls,
    filter_options,
    format_cost,
    full_value,
    match_label,
    modified_label,
    pending_suboperation_count,
    property_heading,
    review_sort_pagination,
    row_neighbours,
    run_confirmation,
    run_status,
    section_toggle_label,
    sort_direction_label,
    sort_from_header_click,
    suggestion_to_row,
    table_update_label,
    truncate,
)


def _suggestion(**kwargs: object) -> Suggestion:
    base: dict[str, object] = {
        "id": 7,
        "run_id": "r",
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


def test_section_toggle_label_states_the_next_action() -> None:
    assert section_toggle_label("filters", False) == "Show filters"
    assert section_toggle_label("filters", True) == "Hide filters"
    assert section_toggle_label("stats", False) == "Show stats"
    assert section_toggle_label("stats", True) == "Hide stats"


def test_truncate_renders_metadata_shapes() -> None:
    assert truncate([{"value": "a"}, {"value": "b"}]) == "a, b"
    assert truncate([{"label": "Topic A", "value": "x"}]) == "Topic A"
    assert truncate(None) == "—"
    assert truncate(True) == "true"


def test_truncate_appends_an_ellipsis_past_the_limit() -> None:
    assert truncate("x" * 200, limit=10) == "x" * 10 + "…"
    assert truncate("short", limit=10) == "short"


def test_full_value_keeps_long_text_and_newlines() -> None:
    paragraph = "\n".join(f"line {index}" for index in range(10)) + "\n" + "x" * 400

    assert full_value(paragraph) == paragraph
    assert full_value([{"value": paragraph}]) == paragraph
    assert full_value(None) == "—"


# --- match_label ------------------------------------------------------------


def test_match_label_maps_the_tri_state() -> None:
    assert match_label(True) == "exact"
    assert match_label(False) == "mismatch"
    assert match_label(None) == "new"


# --- suggestion_to_row ------------------------------------------------------


def test_suggestion_to_row_flattens_the_display_contract() -> None:
    row = suggestion_to_row(_suggestion(entity_title="Title", status=SuggestionStatus.PENDING))
    assert row["id"] == 7
    assert row["shared_id"] == "s1"
    assert row["title"] == "Title"
    assert row["op"] == "update"
    assert row["property"] == "excerpt"
    assert row["proposed"] == "new"
    assert row["current"] == "old"
    assert row["match"] == "mismatch"
    assert row["status"] == "pending"
    assert row["editable"] is True


def test_suggestion_to_row_carries_the_badge_colors() -> None:
    """Vue slots cannot reach ``window.*``, so colors must ride on the row."""
    row = suggestion_to_row(_suggestion(op="delete", property_name=None, match=None, status=SuggestionStatus.DISCARDED))
    assert row["op_color"] == "red"
    assert row["match_color"] == "grey-7"
    assert row["status_color"] == "pink-10"


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


def test_filter_from_controls_maps_values_through() -> None:
    f = filter_from_controls(status="pending", op="update", property_name="excerpt", text=" foo ")
    assert f.status == [SuggestionStatus.PENDING]
    assert f.op == ["update"]
    assert f.property_name == "excerpt"
    assert f.match is None
    assert f.text == "foo"  # trimmed


def test_filter_from_controls_passes_multi_select_lists_through() -> None:
    f = filter_from_controls(status=["pending", "discarded"], op=["update", "delete"])
    assert f.status == [SuggestionStatus.PENDING, SuggestionStatus.DISCARDED]
    assert f.op == ["update", "delete"]


def test_filter_from_controls_maps_empty_lists_to_no_constraint() -> None:
    f = filter_from_controls(status=[], op=[])
    assert f.status is None
    assert f.op is None


def test_filter_options_list_only_present_values_with_counts() -> None:
    labels = {"update": "update", "delete": "delete", "publish": "publish"}
    options = filter_options(labels, {"update": 1240, "delete": 3}, [])
    assert options == {"update": "update (1,240)", "delete": "delete (3)"}


def test_filter_options_keep_a_selected_value_listed_at_zero() -> None:
    labels = {"update": "update", "delete": "delete"}
    options = filter_options(labels, {"update": 2}, ["delete"])
    assert options == {"update": "update (2)", "delete": "delete (0)"}


def test_filter_options_append_a_selected_value_without_a_label() -> None:
    assert filter_options({"a": "A"}, {}, ["b"]) == {"b": "b (0)"}


def test_filter_options_keep_the_label_order() -> None:
    labels = {"pending": "pending", "applied": "applied", "discarded": "discarded"}
    counts = {"discarded": 1, "pending": 5, "applied": 2}
    assert list(filter_options(labels, counts, [])) == ["pending", "applied", "discarded"]


def test_filter_from_controls_whitespace_text_is_no_constraint() -> None:
    assert filter_from_controls(text="   ").text is None


def test_filter_from_controls_keeps_paging() -> None:
    f = filter_from_controls(offset=50, limit=25)
    assert (f.offset, f.limit) == (50, 25)


# --- the LLM cost card ---------------------------------------------------------


def test_a_cost_reads_as_money_with_two_decimals() -> None:
    assert format_cost(0.0) == "$0.00"
    assert format_cost(1.5) == "$1.50"
    assert format_cost(12.3456) == "$12.35"


def test_the_cost_card_reads_the_spend_against_the_budget() -> None:
    assert cost_card_value(1.25, 10.0) == "$1.25 / $10.00"


# --- entity_row ----------------------------------------------------------------


def _entity_row(
    *,
    shared_id: str = "s1",
    language: str | None = None,
    entity_title: str = "One",
    template_names: list[str] | None = None,
    ops: list[str] | None = None,
    properties: list[str] | None = None,
    proposal_count: int = 2,
    pending: int = 2,
    applied: int = 0,
    reverted: int = 0,
    discarded: int = 0,
    match_summary: str = "exact",
    has_outcome: bool = False,
) -> EntityReviewRow:
    return EntityReviewRow(
        shared_id=shared_id,
        language=language,
        entity_title=entity_title,
        template_names=template_names if template_names is not None else ["Judgment"],
        ops=ops if ops is not None else ["update"],
        properties=properties if properties is not None else ["excerpt"],
        proposal_count=proposal_count,
        pending=pending,
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
    assert row["op_colors"] == ["teal-10"]
    assert row["template"] == "Judgment"


def test_entity_row_uses_shared_id_when_there_is_no_title() -> None:
    assert entity_row(_entity_row(entity_title=""))["title"] == "s1"


def test_entity_row_dashes_an_unknown_template() -> None:
    row = entity_row(_entity_row(template_names=[], ops=["delete"]))
    assert row["template"] == "—"
    assert row["op_colors"] == ["red"]


def test_entity_row_mixed_ops_keep_their_colors_aligned() -> None:
    row = entity_row(_entity_row(ops=["update", "delete"]))
    assert row["ops"] == "update, delete"
    assert row["op_colors"] == ["teal-10", "red"]


def test_entity_row_match_column_is_gone() -> None:
    row = entity_row(_entity_row())
    assert "match" not in row
    assert "match_color" not in row
    assert "properties" not in row
    assert "proposals" not in row
    assert "status_summary" not in row


# --- the language column -----------------------------------------------------


def test_entity_row_carries_the_language_and_a_row_key_per_language() -> None:
    """Two languages of one entity are two ROWS: distinct keys, distinct checkboxes."""
    spanish = entity_row(_entity_row(language="es"))
    english = entity_row(_entity_row(language="en"))

    assert (spanish["language"], english["language"]) == ("es", "en")
    assert spanish["row_key"] != english["row_key"]
    assert spanish["shared_id"] == english["shared_id"] == "s1"


def test_a_language_independent_row_shows_an_empty_language() -> None:
    """The '—' group renders as an empty string so the column never says 'None'."""
    row = entity_row(_entity_row(language=None))

    assert row["language"] == ""
    assert row["row_key"] == entity_language_key("s1", None)


def test_the_uwazi_link_opens_the_row_in_its_own_language() -> None:
    """The /<language>/entity/<id> segment is the row's language, not a fixed 'en'."""
    spanish = entity_row(_entity_row(language="es"), "http://localhost:3000")
    agnostic = entity_row(_entity_row(language=None), "http://localhost:3000")

    assert spanish["url"] == "http://localhost:3000/es/entity/s1"
    assert agnostic["url"] == "http://localhost:3000/en/entity/s1"


def test_run_confirmation_names_the_languages_it_will_write() -> None:
    title, _message = run_confirmation(2, ["update"], ["es"])

    assert title == "Apply 2 suggestions (es)"


def test_run_confirmation_without_languages_stays_plain() -> None:
    title, _message = run_confirmation(1, ["delete"])

    assert title == "Apply 1 suggestion"


# --- open in Uwazi ------------------------------------------------------------


def test_entity_open_url_points_at_the_instances_entity_page() -> None:
    item = _entity_row(shared_id="abc", ops=["update"])

    assert entity_open_url(item, "http://localhost:3000") == "http://localhost:3000/en/entity/abc"


def test_entity_open_url_does_not_double_the_slash() -> None:
    item = _entity_row(ops=["delete"])

    assert entity_open_url(item, "http://localhost:3000/") == "http://localhost:3000/en/entity/s1"


def test_entity_open_url_is_none_when_the_instance_is_not_configured() -> None:
    """No UWAZI_URL means no link at all — never a broken one."""
    assert entity_open_url(_entity_row(ops=["update"]), None) is None


def test_entity_open_url_is_none_for_ops_with_nothing_to_open() -> None:
    """A create row still holds its dry-created placeholder: nothing to open."""
    for ops in (["create"], ["publish"], ["unpublish"], ["create_relationships"], ["set_publish_status"]):
        assert entity_open_url(_entity_row(ops=ops), "http://localhost:3000") is None


def test_entity_open_url_covers_a_mixed_row_through_its_existing_op() -> None:
    item = _entity_row(ops=["create", "update"])

    assert entity_open_url(item, "http://localhost:3000") == "http://localhost:3000/en/entity/s1"


def test_entity_row_carries_the_open_url_only_when_there_is_one() -> None:
    assert entity_row(_entity_row(ops=["update"]), "http://localhost:3000")["url"] == "http://localhost:3000/en/entity/s1"
    assert entity_row(_entity_row(ops=["create"]), "http://localhost:3000")["url"] is None
    assert entity_row(_entity_row(ops=["update"]))["url"] is None


# --- the title cell ------------------------------------------------------------


def test_entity_row_trims_a_long_title_and_keeps_the_full_text() -> None:
    """A document title is far wider than its column: cut it, keep the tooltip."""
    title = "A/HRC/37/NGO/9 " + ("x" * 400)

    row = entity_row(_entity_row(entity_title=title))

    expected = truncate(title, TITLE_CELL_CHARS)
    assert row["title"] == expected
    assert len(expected) == TITLE_CELL_CHARS + 1  # TITLE_CELL_CHARS chars + the ellipsis
    assert row["title_full"] == title


def test_entity_row_leaves_a_short_title_untouched() -> None:
    row = entity_row(_entity_row(entity_title="A/HRC/37/NGO/9"))

    assert row["title"] == row["title_full"] == "A/HRC/37/NGO/9"


def test_the_shared_id_is_its_own_field_and_is_not_glued_to_the_title() -> None:
    """The cell shows 'id title'; the id must be selectable (and copyable) on its own."""
    row = entity_row(_entity_row(shared_id="abc123", entity_title="One"))

    assert row["shared_id"] == "abc123"
    assert row["title"] == "One"
    assert "abc123" not in row["title"]
    assert "(" not in row["title"]


def test_entity_row_trims_the_template_list() -> None:
    names = ", ".join(f"TEMPLATE-{index}" for index in range(10))

    row = entity_row(_entity_row(template_names=names.split(", ")))

    assert len(row["template"]) < len(names)
    assert row["template"].endswith("…")


def test_entity_row_carries_the_url_for_the_title_cell() -> None:
    """The title cell is the link itself; the row still carries the URL."""
    assert entity_row(_entity_row(ops=["update"]), "http://localhost:3000")["url"].endswith("/en/entity/s1")


# --- property labels -----------------------------------------------------------


def test_property_heading_prefers_the_templates_label() -> None:
    """``document_ref`` is an identifier; Uwazi — and the operator — reads "Document Ref"."""
    labels = {"document_ref": "Document Ref"}

    assert property_heading("document_ref", "update", labels) == "Document Ref"


def test_property_heading_falls_back_to_the_name_without_labels() -> None:
    """An unreachable instance keeps the names it always showed."""
    assert property_heading("document_ref", "update", {}) == "document_ref"


def test_property_heading_names_a_title_change() -> None:
    """``_title`` is the review table's synthetic name; the operator reads "Title"."""
    assert property_heading(TITLE_PROPERTY, "update", {}) == "Title"


def test_property_heading_names_a_whole_entity_row_by_its_op() -> None:
    assert property_heading(None, "delete", {}) == "delete"


def test_run_status_is_pending_for_an_unreviewed_entity() -> None:
    assert run_status(_entity_row(pending=2, applied=0)) == ("pending", "grey-8")


def test_run_status_is_applied_when_every_row_is_applied() -> None:
    assert run_status(_entity_row(pending=0, applied=2)) == ("applied", "blue-9")


def test_run_status_is_reverted_once_a_revert_undid_the_writes() -> None:
    assert run_status(_entity_row(pending=0, applied=0, reverted=2)) == ("reverted", "indigo")


def test_run_status_stays_pending_on_a_partial_state() -> None:
    # One applied + one reverted: work remains, and it is not fully applied.
    assert run_status(_entity_row(pending=0, applied=1, reverted=1)) == ("pending", "grey-8")


def test_pending_card_counts_the_pending_suggestions() -> None:
    counts = {
        "subs_pending": 2,
        "subs_applied": 4,
        "subs_reverted": 5,
        "subs_discarded": 6,
    }

    assert pending_suboperation_count(counts) == 2


def test_pending_card_is_zero_for_an_empty_set() -> None:
    assert pending_suboperation_count({}) == 0


def test_filter_from_controls_passes_shared_ids_through() -> None:
    f = filter_from_controls(shared_ids=["a", "b"])
    assert f.shared_ids == ["a", "b"]
    assert filter_from_controls().shared_ids is None


# --- table refresh label -----------------------------------------------------


def test_table_update_label_is_silent_for_a_never_synced_table() -> None:
    """A never-synced run says nothing: the drift banner already reports the check."""
    assert table_update_label(None) == ""


def test_table_update_label_shows_date_and_time() -> None:
    from datetime import datetime

    assert table_update_label(datetime(2026, 10, 22, 14, 5)) == "Synced with Uwazi 2026-10-22 14:05 UTC"


def test_table_update_label_tolerates_an_unexpected_value() -> None:
    assert table_update_label(object()) == "Synced with Uwazi"


# --- modified column + sort control ------------------------------------------


def test_modified_label_formats_a_timestamp() -> None:
    value = datetime(2026, 11, 3, 9, 45, tzinfo=timezone.utc)
    assert modified_label(value) == "2026-11-03 09:45"


def test_modified_label_is_empty_until_an_action_happens() -> None:
    """A freshly recorded suggestion has no review activity, so the cell is blank."""
    assert modified_label(None) == ""
    assert modified_label(object()) == ""


def test_sort_direction_label_speaks_the_modified_order() -> None:
    assert sort_direction_label("modified", True) == "Newest first"
    assert sort_direction_label("modified", False) == "Oldest first"
    assert sort_direction_label("recorded", False) == "First recorded first"
    assert sort_direction_label("recorded", True) == "Last recorded first"
    assert sort_direction_label("title", False) == "A → Z"
    assert sort_direction_label("title", True) == "Z → A"


def test_filter_from_controls_passes_the_sort_through() -> None:
    f = filter_from_controls(sort_by="modified", sort_desc=True)
    assert (f.sort_by, f.sort_desc) == ("modified", True)
    assert (filter_from_controls().sort_by, filter_from_controls().sort_desc) == ("recorded", False)


def test_header_click_toggles_the_same_column_between_two_directions() -> None:
    assert sort_from_header_click("title", False, "title") == ("title", True)
    assert sort_from_header_click("title", True, "title") == ("title", False)
    # The Status header reports "run_status" to Quasar but sorts on "status".
    assert sort_from_header_click("status", True, "run_status") == ("status", False)
    assert sort_from_header_click("modified", True, "modified") == ("modified", False)


def test_header_click_on_a_new_column_starts_in_its_natural_direction() -> None:
    assert sort_from_header_click("recorded", False, "title") == ("title", False)
    assert sort_from_header_click("title", False, "modified") == ("modified", True)
    assert sort_from_header_click("modified", True, "template") == ("template", False)
    assert sort_from_header_click("title", True, "ops") == ("ops", False)


def test_header_click_ignores_headers_that_cannot_sort() -> None:
    assert sort_from_header_click("recorded", False, "expand") is None
    assert sort_from_header_click("title", False, "actions") is None
    assert sort_from_header_click("title", False, "not-a-column") is None


def test_sortable_headers_cover_every_sort_key_except_original_order() -> None:
    assert set(SORT_KEY_BY_COLUMN.values()) == set(OPERATION_SORT_OPTIONS) - {"recorded"}
    assert COLUMN_BY_SORT_KEY["status"] == "run_status"
    assert COLUMN_BY_SORT_KEY["modified"] == "modified"


def test_review_sort_pagination_mirrors_the_server_sort_for_quasar() -> None:
    assert review_sort_pagination("modified", True, 41) == {
        "rowsPerPage": 0,
        "rowsNumber": 41,
        "sortBy": "modified",
        "descending": True,
    }
    assert review_sort_pagination("status", False)["sortBy"] == "run_status"
    assert review_sort_pagination("recorded", False, 3)["sortBy"] == ""
    assert review_sort_pagination("title", False)["rowsNumber"] == 0


def test_row_neighbours_has_no_buttons_for_a_single_row() -> None:
    assert row_neighbours([("a", None)], ("a", None)) == (None, None)


def test_row_neighbours_walks_both_directions_in_the_middle() -> None:
    pairs = [("a", None), ("b", "en"), ("c", None)]
    assert row_neighbours(pairs, ("b", "en")) == (("a", None), ("c", None))


def test_row_neighbours_hides_the_previous_at_the_first_row() -> None:
    pairs = [("a", None), ("b", None)]
    assert row_neighbours(pairs, ("a", None)) == (None, ("b", None))


def test_row_neighbours_hides_the_next_at_the_last_row() -> None:
    pairs = [("a", None), ("b", None)]
    assert row_neighbours(pairs, ("b", None)) == (("a", None), None)


def test_row_neighbours_never_wraps_a_missing_current_row() -> None:
    assert row_neighbours([("a", None), ("b", None)], ("missing", None)) == (None, None)
