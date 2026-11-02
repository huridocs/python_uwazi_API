"""Isolated unit tests for choosing which entities a Run may snapshot by property (pure)."""

from __future__ import annotations

from uwazi_admin_agent.domain.suggestion import Suggestion
from uwazi_admin_agent.use_cases.apply_suggestions_use_case import property_scope_ids


def _update(shared_id: str) -> dict:
    return {"shared_id": shared_id, "language": "en", "metadata": {"a": [{"value": "x"}]}}


def _row(shared_id: str, op: str, proposed: object = None) -> Suggestion:
    return Suggestion(
        id=1,
        run_id="run-1",
        shared_id=shared_id,
        op=op,
        status="accepted",
        proposed_value=proposed,
        template_name="Template",
    )


def test_a_pure_property_update_is_property_scoped() -> None:
    assert property_scope_ids([_update("a"), _update("b")], []) == {"a", "b"}


def test_an_entity_with_any_other_write_keeps_the_full_snapshot() -> None:
    scoped = property_scope_ids([_update("a"), _update("b")], [_row("a", "delete")])

    assert scoped == {"b"}


def test_every_whole_entity_op_disqualifies_the_entity() -> None:
    rows = [_row("a", "delete"), _row("b", "publish"), _row("c", "unpublish"), _row("d", "set_publish_status")]

    assert property_scope_ids([_update("a"), _update("b"), _update("c"), _update("d")], rows) == set()


def test_a_merge_disqualifies_its_target_and_every_source() -> None:
    scoped = property_scope_ids(
        [_update("target"), _update("source"), _update("untouched")],
        [],
        [("target", ["source", "other-source"])],
    )

    assert scoped == {"untouched"}


def test_both_endpoints_of_a_relationship_row_keep_full_snapshots() -> None:
    relationship = _row(
        "from",
        "create_relationships",
        {"from_entity_shared_id": "from", "to_entity_shared_id": "to"},
    )

    scoped = property_scope_ids([_update("from"), _update("to"), _update("elsewhere")], [relationship])

    assert scoped == {"elsewhere"}


def test_an_entity_with_no_update_payload_is_never_scoped() -> None:
    assert property_scope_ids([], [_row("a", "delete")]) == set()
