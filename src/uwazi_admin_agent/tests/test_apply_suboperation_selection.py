"""Isolated unit tests for the suboperation (row-id) selection in the apply path (pure)."""

from __future__ import annotations

from uwazi_admin_agent.domain.suggestion import Suggestion, SuggestionStatus
from uwazi_admin_agent.use_cases.apply_suggestions_use_case import (
    build_apply_creates,
    build_apply_updates,
    partition_for_apply,
    pushable_rows,
)


def _row(
    row_id: int,
    *,
    shared_id: str = "entity-a",
    op: str = "update",
    property_name: str | None = "p1",
    language: str | None = "en",
    status: SuggestionStatus = SuggestionStatus.ACCEPTED,
    proposed: object = "new",
) -> Suggestion:
    return Suggestion(
        id=row_id,
        run_id="run-1",
        attempt=1,
        shared_id=shared_id,
        op=op,
        property_name=property_name,
        language=language,
        status=status,
        proposed_value=proposed,
        template_name="Template",
    )


def test_row_ids_narrow_the_pushable_rows_below_the_entity_language_pair() -> None:
    rows = [
        _row(1, property_name="p1"),
        _row(2, property_name="p2"),
        _row(3, property_name="p1", language="es"),
    ]
    selected = pushable_rows(rows, selection=[("entity-a", "en")], row_ids=[2])
    assert [row.id for row in selected] == [2]


def test_no_row_ids_keeps_the_pair_level_behaviour() -> None:
    rows = [_row(1, property_name="p1"), _row(2, property_name="p2"), _row(3, language="es")]
    selected = pushable_rows(rows, selection=[("entity-a", "en")])
    assert [row.id for row in selected] == [1, 2]


def test_an_empty_row_id_list_runs_nothing() -> None:
    rows = [_row(1)]
    assert pushable_rows(rows, row_ids=[]) == []


def test_build_apply_updates_writes_only_the_selected_property() -> None:
    rows = [_row(1, property_name="p1"), _row(2, property_name="p2")]
    updates = build_apply_updates(rows, row_ids=[1])
    assert updates == [
        {
            "shared_id": "entity-a",
            "language": "en",
            "metadata": {"p1": [{"value": "new", "language": "en"}]},
            "template_name": "Template",
        }
    ]


def test_partition_for_apply_honours_row_ids() -> None:
    rows = [
        _row(1, property_name="p1"),
        _row(2, op="delete", property_name=None, language=None),
        _row(3, op="create", property_name="p1", shared_id="dry-created-1"),
    ]
    properties, whole_entity = partition_for_apply(rows, row_ids=[1])
    assert [row.id for row in properties] == [1]
    assert whole_entity == []


def test_build_apply_creates_groups_only_the_selected_rows() -> None:
    rows = [
        _row(1, op="create", shared_id="dry-created-1", property_name="p1"),
        _row(2, op="create", shared_id="dry-created-1", property_name="p2"),
    ]
    creates = build_apply_creates(rows, row_ids=[2])
    assert creates == [
        {
            "title": "",
            "template_name": "Template",
            "metadata": {"p2": [{"value": "new", "language": "en"}]},
            "language": "en",
            "source_shared_id": "dry-created-1",
        }
    ]


def test_relationship_rows_are_pushed_with_their_payload() -> None:
    link = {"from_entity_shared_id": "entity-a", "to_entity_shared_id": "entity-b", "relationship_type": "ref"}
    row = _row(9, op="create_relationships", property_name=None, language=None, proposed=link)
    assert pushable_rows([row], row_ids=[9]) == [row]


def test_a_pending_row_is_not_pushed_even_when_its_id_is_selected() -> None:
    row = _row(4, status=SuggestionStatus.PENDING)
    assert pushable_rows([row], row_ids=[4]) == []
