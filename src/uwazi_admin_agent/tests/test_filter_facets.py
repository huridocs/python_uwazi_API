"""Isolated tests for the review page's faceted filters and create-row display.

Pure functions only: option builders over literal counts, and the operation-row
payload over literal ``OperationReviewRow``/``Suggestion`` models. No mocks, no
store, no network (per AGENTS.md).
"""

from __future__ import annotations

from datetime import datetime, timezone

from uwazi_admin_agent.domain.suggestion import OperationReviewRow, Suggestion
from uwazi_admin_agent.drivers.web.operations_view import operation_open_url, operation_row
from uwazi_admin_agent.drivers.web.review_view import (
    filter_options,
    language_filter_options,
    property_filter_options,
)

NOW = datetime(2026, 11, 25, tzinfo=timezone.utc)


def _sub(**overrides) -> Suggestion:
    values = {
        "run_id": "r",
        "shared_id": "dry-created-0",
        "entity_title": "Alpha",
        "template_name": "Rec",
        "op": "create",
        "property_name": "title",
        "status": "pending",
        "proposed_value": "Alpha",
        "created_at": NOW,
    }
    values.update(overrides)
    return Suggestion(**values)


def _operation(subs: list[Suggestion], **overrides) -> OperationReviewRow:
    values = {
        "shared_id": subs[0].shared_id,
        "entity_title": subs[0].entity_title,
        "template_names": ["Rec"],
        "ops": list(dict.fromkeys(sub.op for sub in subs)),
        "properties": [sub.property_name for sub in subs if sub.property_name],
        "proposal_count": len(subs),
        "pending": len(subs),
        "applied": 0,
        "reverted": 0,
        "discarded": 0,
        "match_summary": "new",
        "has_outcome": True,
        "modified_at": NOW,
        "subs": subs,
    }
    values.update(overrides)
    return OperationReviewRow(**values)


# --- option builders --------------------------------------------------------


def test_a_zero_count_option_is_not_offered() -> None:
    options = filter_options({"update": "update", "delete": "delete"}, {"update": 3, "delete": 0}, [])
    assert options == {"update": "update (3)"}


def test_a_selected_option_stays_listed_at_zero_so_the_filter_is_never_cleared() -> None:
    options = filter_options({"update": "update", "delete": "delete"}, {"update": 3, "delete": 0}, ["delete"])
    assert options["delete"] == "delete (0)"
    assert options["update"] == "update (3)"


def test_the_property_dropdown_keeps_any_property_and_counts_the_rest() -> None:
    options = property_filter_options({"rec": "Rec"}, {"rec": 4, "other": 0}, "")
    assert options == {"": "any property", "rec": "Rec (4)"}


def test_a_selected_property_that_lost_every_row_is_kept_at_zero() -> None:
    options = property_filter_options({"rec": "Rec"}, {}, "rec")
    assert options["rec"] == "Rec (0)"


def test_the_language_dropdown_uses_the_same_rules() -> None:
    assert language_filter_options({"es": 2}, "") == {"": "any language", "es": "es (2)"}
    assert language_filter_options({"es": 0}, "") == {"": "any language"}


# --- create-row display -----------------------------------------------------


def test_a_create_row_shows_its_proposed_title_not_the_placeholder() -> None:
    row = operation_row(_operation([_sub()]))
    assert row["title_full"] == "Alpha"
    assert "display_shared_id" not in row
    assert row["url"] is None


def test_a_titleless_create_names_what_it_will_be() -> None:
    row = operation_row(_operation([_sub(entity_title="", proposed_value=None, property_name=None)]))
    assert row["title_full"] == "New Rec entity"
    assert "dry-created" not in row["title_full"]


def test_an_applied_create_shows_the_minted_id_and_links_to_it() -> None:
    sub = _sub(created_shared_id="REAL123", status="applied")
    row = operation_row(_operation([sub]), uwazi_url="http://instance")
    assert row["created_shared_id"] == "REAL123"
    assert row["url"] == "http://instance/en/entity/REAL123"


def test_an_unrun_create_has_no_link_to_an_entity_that_does_not_exist() -> None:
    assert operation_open_url("dry-created-0", ["create"], ["en"], "http://instance") is None


def test_a_non_create_row_still_links_to_its_own_entity() -> None:
    assert operation_open_url("abc", ["update"], ["es"], "http://instance") == "http://instance/es/entity/abc"


# --- store-level facets -----------------------------------------------------


def test_the_store_counts_each_dimension_against_the_other_filters(tmp_path) -> None:
    from uwazi_admin_agent.adapters.sqlite_suggestion_store import SqliteSuggestionStore
    from uwazi_admin_agent.domain.suggestion import SuggestionFilter

    store = SqliteSuggestionStore(tmp_path / "facets.db")
    store.add_suggestions(
        [
            _sub(shared_id="a", op="update", property_name="p1", status="pending"),
            _sub(shared_id="b", op="update", property_name="p2", status="pending"),
            _sub(shared_id="c", op="delete", property_name=None, status="discarded"),
        ]
    )
    whole = store.op_status_counts("r")
    assert whole["op"] == {"update": 2, "delete": 1}

    # Restricting to update narrows the OTHER dimensions, but not its own
    # options (a choice must not cancel itself out).
    narrowed = store.op_status_counts("r", SuggestionFilter(op=["update"], limit=1000))
    # Its OWN dimension keeps every option (the chosen "update" must not hide
    # the alternatives), while the others narrow to what the choice leaves.
    assert narrowed["op"] == {"update": 2, "delete": 1}
    assert narrowed["status"] == {"pending": 2}
    assert narrowed["property"] == {"p1": 1, "p2": 1}
