"""Isolated unit tests for the operations/suboperations table helpers (pure)."""

from __future__ import annotations

from datetime import datetime

from uwazi_admin_agent.domain.suggestion import (
    OperationReviewRow,
    Suggestion,
    SuggestionStatus,
)
from uwazi_admin_agent.drivers.web import operations_view as view


def _sub(
    sub_id: int | None = 1,
    *,
    op: str = "update",
    property_name: str | None = "document_ref",
    language: str | None = "en",
    shared_id: str = "entity-a",
    status: SuggestionStatus = SuggestionStatus.PENDING,
    proposed: object = "new",
    current: object = "old",
    match: bool | None = None,
    detail_key: str | None = None,
) -> Suggestion:
    return Suggestion(
        id=sub_id,
        run_id="run-1",
        shared_id=shared_id,
        op=op,
        property_name=property_name,
        language=language,
        status=status,
        proposed_value=proposed,
        current_value=current,
        match=match,
        detail_key=detail_key,
    )


def _operation(
    subs: list[Suggestion],
    *,
    shared_id: str = "entity-a",
    ops: list[str] | None = None,
    languages: list[str] | None = None,
    applied: int = 0,
) -> OperationReviewRow:
    return OperationReviewRow(
        shared_id=shared_id,
        entity_title="A title",
        template_names=["Template"],
        ops=ops or ["update"],
        properties=["document_ref"],
        languages=languages or ["en"],
        proposal_count=len(subs),
        pending=len(subs) - applied,
        applied=applied,
        reverted=0,
        discarded=0,
        match_summary="n/a",
        has_outcome=True,
        subs=subs,
    )


def test_lock_note_names_the_reason_only_for_whole_entity_ops() -> None:
    assert view.lock_note("update") == ""
    assert view.lock_note("create_relationships") == ""
    assert "one action" in view.lock_note("delete")
    assert "one Run" in view.lock_note("update_and_delete")
    assert view.is_selectable("update")
    assert not view.is_selectable("delete")


def test_sub_label_names_the_link_target() -> None:
    sub = _sub(
        op="create_relationships",
        property_name=None,
        language=None,
        proposed={"to_entity_shared_id": "target-9", "relationship_type_name": "References"},
        detail_key="target-9",
    )
    assert view.sub_label(sub) == "→ target-9"
    assert view.sub_caption(sub) == "References"
    assert view.sub_values(sub) == ("—", "→ target-9 (References)")


def test_sub_label_and_values_for_a_whole_entity_op() -> None:
    sub = _sub(op="delete", property_name=None, language=None, proposed=None)
    assert view.sub_label(sub) == "Delete"
    assert view.sub_values(sub) == ("—", "—")
    assert view.sub_match(sub) == "n/a"


def test_sub_label_uses_the_property_label_and_never_shows_the_internal_name() -> None:
    """The label is the heading; the internal property name is not operator-facing."""
    sub = _sub(property_name="document_ref")
    assert view.sub_label(sub) == "document_ref"
    assert view.sub_label(sub, {"document_ref": "Document Ref"}) == "Document Ref"
    assert view.sub_caption(sub) == ""
    assert view.sub_caption(sub, {"document_ref": "Document Ref"}) == ""


def test_set_publish_status_value_follows_the_flag() -> None:
    assert view.sub_values(_sub(op="set_publish_status", property_name=None, proposed=True))[1] == "publish"
    assert view.sub_values(_sub(op="set_publish_status", property_name=None, proposed=False))[1] == "unpublish"


def test_sub_status_vocabulary_uses_applied_for_written_rows() -> None:
    assert view.sub_status_label(SuggestionStatus.APPLIED) == "applied"
    assert view.sub_status_color(SuggestionStatus.APPLIED) == "blue-9"
    assert view.sub_status_label(SuggestionStatus.DISCARDED) == "discarded"
    assert view.sub_status_label(SuggestionStatus.REVERTED) == "reverted"


def test_sub_status_vocabulary_has_one_word_per_store_state() -> None:
    """Each lifecycle state has exactly one badge word — no accept/edit aliases."""
    assert view.sub_status_label(SuggestionStatus.PENDING) == "pending"
    assert view.sub_status_color(SuggestionStatus.PENDING) == "grey-8"
    assert view.sub_status_label(SuggestionStatus.APPLIED) == "applied"
    assert view.sub_status_label(SuggestionStatus.REVERTED) == "reverted"
    assert view.sub_status_label(SuggestionStatus.DISCARDED) == "discarded"


def test_operation_status_is_derived_from_its_parts() -> None:
    assert view.operation_status([{"status": "applied"}, {"status": "applied"}]) == "applied"
    assert view.operation_status([{"status": "discarded"}, {"status": "discarded"}]) == "discarded"
    assert view.operation_status([{"status": "reverted"}, {"status": "reverted"}]) == "reverted"
    assert view.operation_status([{"status": "applied"}, {"status": "pending"}]) == "mixed"
    assert view.operation_status([{"status": "pending"}, {"status": "discarded"}]) == "pending"
    assert view.operation_status([]) == "pending"


def test_only_pending_and_reverted_parts_are_RUN_selectable() -> None:
    """An applied/discarded part can never inflate Run (N) / Discard (N)."""
    pending = view.suboperation_row(_sub(1, status=SuggestionStatus.PENDING), checked=True, in_place=True)
    reverted = view.suboperation_row(_sub(2, status=SuggestionStatus.REVERTED), checked=True, in_place=True)
    applied = view.suboperation_row(_sub(3, status=SuggestionStatus.APPLIED), checked=True, in_place=True)
    discarded = view.suboperation_row(_sub(4, status=SuggestionStatus.DISCARDED), checked=True, in_place=True)

    assert (pending["runnable"], pending["run_selectable"], pending["checked"]) == (True, True, True)
    assert (reverted["runnable"], reverted["run_selectable"], reverted["checked"]) == (True, True, True)
    assert (applied["runnable"], applied["run_selectable"], applied["revert_selectable"]) == (False, False, True)
    assert (discarded["runnable"], discarded["run_selectable"], discarded["revert_selectable"]) == (False, False, False)
    assert discarded["note"] == "discarded — restore it first"


def test_an_applied_part_is_selectable_for_the_revert() -> None:
    """The top Revert acts on the checked entities, so a written row must be
    checkable — it used to have no checkbox at all ("no runnable suboperation")."""
    applied = view.suboperation_row(_sub(3, status=SuggestionStatus.APPLIED), checked=True, in_place=True)

    assert applied["selectable"] is True
    assert applied["checked"] is True
    assert applied["revert_selectable"] is True
    assert applied["note"] == "already applied — select it to revert this write"


def test_an_applied_part_of_a_run_level_undo_op_is_not_selectable() -> None:
    """A delete / create-merge reverts at run level, so its parts stay excluded."""
    applied_delete = view.suboperation_row(
        _sub(1, op="delete", property_name=None, language=None, status=SuggestionStatus.APPLIED),
        checked=True,
        in_place=False,
    )

    assert applied_delete["selectable"] is False
    assert applied_delete["revert_selectable"] is False


def test_a_locked_part_stays_independently_unselectable() -> None:
    """A delete part runs with its operation even when that operation is runnable."""
    locked = view.suboperation_row(
        _sub(1, op="delete", property_name=None, language=None, status=SuggestionStatus.PENDING),
        checked=False,
        in_place=False,
    )

    assert locked["runnable"] is True
    assert locked["independent"] is False
    assert locked["selectable"] is False


def test_operation_row_exposes_whether_any_part_is_runnable() -> None:
    item = _operation([_sub(1, status=SuggestionStatus.APPLIED)], applied=1)
    applied = view.operation_row(item)

    pending_item = _operation([_sub(2, status=SuggestionStatus.PENDING)], applied=0)
    pending = view.operation_row(pending_item)

    assert applied["has_runnable"] is False
    assert pending["has_runnable"] is True


def test_validated_counts_only_checkable_parts() -> None:
    payload = view.suboperation_row(_sub(1), checked=True, in_place=True)
    locked = view.suboperation_row(_sub(2, op="delete", property_name=None, language=None), checked=True, in_place=False)
    assert payload["checked"] is True
    assert locked["checked"] is False
    assert view.validated_counts([payload, locked]) == (1, 1)
    assert view.validated_cell([payload, locked]) == ("1/1", "green")
    assert view.validated_cell([locked]) == ("n/a", "grey-4")


def test_sub_revertable_only_for_an_applied_property_row_of_a_modified_entity() -> None:
    applied = _sub(1, status=SuggestionStatus.APPLIED)
    pending = _sub(2, status=SuggestionStatus.PENDING)
    assert view.sub_revertable(applied, in_place=True) is True
    assert view.sub_revertable(applied, in_place=False) is False
    assert view.sub_revertable(pending, in_place=True) is False
    locked = _sub(3, op="delete", property_name=None, status=SuggestionStatus.APPLIED)
    assert view.sub_revertable(locked, in_place=True) is False


def test_operation_row_combines_parts_and_derives_the_parent_badge() -> None:
    item = _operation(
        [
            _sub(1, status=SuggestionStatus.APPLIED, match=True),
            _sub(2, status=SuggestionStatus.APPLIED, language="es", match=True),
        ],
        applied=2,
    )
    row = view.operation_row(item, checked_ids=[1, 2], uwazi_url="https://uwazi.example")
    assert row["row_key"] == "entity-a"
    assert row["run_status"] == "applied"
    # Applied parts are checkable (for the Revert) but not runnable: the counter
    # beside the status badge reads how many of the ACTIONABLE parts are ticked,
    # and Run (N) counts only the runnable side of the same selection.
    assert row["validated"] == "2/2"
    assert row["has_runnable"] is False
    assert row["has_selectable"] is True
    assert row["revertable"] is True
    assert row["url"] == "https://uwazi.example/en/entity/entity-a"
    assert [sub["language"] for sub in row["subs"]] == ["en", "es"]

    pending_item = _operation(
        [
            _sub(3, status=SuggestionStatus.PENDING, match=True),
            _sub(4, status=SuggestionStatus.PENDING, language="es", match=True),
        ]
    )
    pending = view.operation_row(pending_item, checked_ids=[3, 4])
    assert pending["validated"] == "2/2"
    partial = view.operation_row(pending_item, checked_ids=[3])
    assert partial["validated"] == "1/2"
    assert partial["run_status"] == "pending"


def test_operation_row_locks_the_parts_of_a_delete() -> None:
    item = _operation([_sub(7, op="delete", property_name=None, language=None)], ops=["delete"], languages=[])
    row = view.operation_row(item, checked_ids=[7])
    assert row["subs"][0]["selectable"] is False
    assert row["subs"][0]["checked"] is False
    assert row["has_checkable"] is False
    assert row["validated"] == "n/a"
    assert row["revertable"] is False
    assert row["url"] is None


def test_sub_values_render_a_date_property_as_a_calendar_date() -> None:
    sub = _sub(proposed=1700086400, current=1700000000)

    current, proposed = view.sub_values(sub, {"document_ref": "date"})

    assert (current, proposed) == ("2023-11-14", "2023-11-15")


def test_sub_values_without_types_keep_the_raw_value() -> None:
    sub = _sub(proposed=1700086400, current=1700000000)

    assert view.sub_values(sub) == ("1700000000", "1700086400")


def test_operation_row_renders_date_suboperations_with_the_template_types() -> None:
    item = _operation([_sub(1, proposed=1700000000, current=1690000000)])

    row = view.operation_row(item, checked_ids=[1], types={"document_ref": "date"})

    assert row["subs"][0]["current"] == "2023-07-22"
    assert row["subs"][0]["proposed"] == "2023-11-14"


def test_refs_helpers_deduplicate_and_keep_order() -> None:
    refs = [
        (3, "entity-a", "en", "update"),
        (4, "entity-a", "es", "update"),
        (5, "entity-a", None, "delete"),
        (3, "entity-a", "en", "update"),
    ]
    assert view.ids_of_refs(refs) == [3, 4, 5]
    assert view.pairs_of_refs(refs) == [("entity-a", "en"), ("entity-a", "es"), ("entity-a", None)]
    assert view.ops_of_refs(refs) == ["update", "delete"]
    assert view.languages_of_refs(refs) == ["en", "es", "—"]
    assert view.entities_of_refs(refs) == ["entity-a"]


def test_merge_operation_offers_the_operation_revert_but_no_per_part_revert() -> None:
    """A merge row is undone as ONE operation; its parts stay non-revertable."""
    subs = [
        _sub(1, op="update_and_delete", status=SuggestionStatus.APPLIED),
        _sub(2, op="update_and_delete", property_name="excerpt", status=SuggestionStatus.APPLIED),
    ]
    item = _operation(subs, ops=["update_and_delete"], applied=2)

    row = view.operation_row(item, checked_ids=[])

    assert row["in_place"] is True
    assert row["revertable"] is True
    assert [sub["revertable"] for sub in row["subs"]] == [False, False]


def test_create_merge_operation_is_not_revertable_from_its_row() -> None:
    """A create-merge's target is a dry-created placeholder: run-level undo only."""
    subs = [_sub(1, op="create_and_delete", status=SuggestionStatus.APPLIED)]
    item = _operation(subs, shared_id="dry-created-0", ops=["create_and_delete"], applied=1)

    row = view.operation_row(item, checked_ids=[])

    assert row["revertable"] is False
    assert [sub["revertable"] for sub in row["subs"]] == [False]


# --- multiselect value-level payload ------------------------------------------


def test_suboperation_row_carries_multiselect_value_decisions() -> None:
    sub = _sub(1, property_name="keywords", proposed=["A", "B"])
    sub = sub.model_copy(update={"rejected_values": ["B"]})
    row = view.suboperation_row(sub, checked=True, in_place=True, types={"keywords": "multiselect"})
    assert row["select_type"] == "multiselect"
    assert row["values"] == [{"label": "A", "accepted": True}, {"label": "B", "accepted": False}]
    assert row["has_rejected"] is True


def test_suboperation_row_values_are_empty_for_a_text_row() -> None:
    sub = _sub(1, property_name="excerpt", proposed="new text")
    row = view.suboperation_row(sub, checked=True, in_place=True, types={"excerpt": "text"})
    assert row["values"] == []
    assert row["has_rejected"] is False
    assert row["select_type"] is None


def test_suboperation_row_multiselect_without_rejections_reads_all_accepted() -> None:
    sub = _sub(1, property_name="keywords", proposed=["A", "B"])
    row = view.suboperation_row(sub, checked=True, in_place=True, types={"keywords": "multiselect"})
    assert row["values"] == [{"label": "A", "accepted": True}, {"label": "B", "accepted": True}]
    assert row["has_rejected"] is False
    # The chips ARE the written set; no second line repeats them.
    assert row["extra_selection"] == ""


def test_suboperation_row_reports_the_operator_selection_as_the_written_set() -> None:
    sub = _sub(1, property_name="keywords", proposed=["A", "B"])
    sub = sub.model_copy(update={"selected_values": ["A", "C"]})
    row = view.suboperation_row(sub, checked=True, in_place=True, types={"keywords": "multiselect"})
    # The chips are the PROPOSAL (B stays visible, marked dropped); the value the
    # operator ADDED (C) has no chip, so it is the one extra line.
    assert row["values"] == [{"label": "A", "accepted": True}, {"label": "B", "accepted": False}]
    assert row["has_rejected"] is True
    assert row["extra_selection"] == "C"


def test_suboperation_row_reads_an_empty_selection_as_nothing_written() -> None:
    sub = _sub(1, property_name="keywords", proposed=["A", "B"])
    sub = sub.model_copy(update={"selected_values": []})
    row = view.suboperation_row(sub, checked=True, in_place=True, types={"keywords": "multiselect"})
    assert [value["accepted"] for value in row["values"]] == [False, False]
    assert row["extra_selection"] == ""


# --- modified column ---------------------------------------------------------


def test_operation_row_carries_the_modified_label() -> None:
    item = _operation([_sub(1)]).model_copy(update={"modified_at": datetime(2026, 11, 3, 9, 45)})

    row = view.operation_row(item)

    assert row["modified"] == "2026-11-03 09:45"


def test_operation_row_reads_blank_when_no_action_was_recorded() -> None:
    assert view.operation_row(_operation([_sub(1)]))["modified"] == ""


# --- reverted rows whose undo re-created an entity ---------------------------


def test_a_reverted_delete_cannot_be_run_again() -> None:
    item = _operation(
        [_sub(1, op="delete", property_name=None, language=None, status=SuggestionStatus.REVERTED)],
        ops=["delete"],
        languages=[],
    )

    row = view.operation_row(item)

    assert row["run_status"] == "reverted"
    assert row["rerun_blocked"] is True
    assert row["has_runnable"] is False
    assert "new sharedId" in row["rerun_blocked_reason"]
    assert row["run_title"] == row["rerun_blocked_reason"]
    assert row["subs"][0]["rerun_blocked"] is True
    assert row["subs"][0]["runnable"] is False


def test_a_reverted_merge_cannot_be_run_again() -> None:
    item = _operation(
        [
            _sub(
                1,
                op="update_and_delete",
                status=SuggestionStatus.REVERTED,
            )
        ],
        ops=["update_and_delete"],
    )

    row = view.operation_row(item)

    assert row["rerun_blocked"] is True
    assert row["has_runnable"] is False
    assert "new sharedIds" in row["rerun_blocked_reason"]


def test_a_reverted_update_can_still_be_run_again() -> None:
    item = _operation([_sub(1, status=SuggestionStatus.REVERTED)])

    row = view.operation_row(item)

    assert row["rerun_blocked"] is False
    assert row["has_runnable"] is True


def test_a_pending_delete_is_still_runnable() -> None:
    item = _operation(
        [_sub(1, op="delete", property_name=None, language=None, status=SuggestionStatus.PENDING)],
        ops=["delete"],
        languages=[],
    )

    row = view.operation_row(item)

    assert row["rerun_blocked"] is False
    assert row["has_runnable"] is True


def test_rerun_blocked_only_matches_a_reverted_recreating_op() -> None:
    assert view.rerun_blocked("delete", "reverted") is True
    assert view.rerun_blocked("update_and_delete", "reverted") is True
    assert view.rerun_blocked("create_and_delete", "reverted") is True
    assert view.rerun_blocked("delete", "pending") is False
    assert view.rerun_blocked("update", "reverted") is False
    assert view.rerun_blocked("create", "reverted") is False


def test_suboperation_row_carries_the_context() -> None:
    sub = _sub(1)
    sub = sub.model_copy(update={"context": "the paragraph it was read from"})

    row = view.suboperation_row(sub, checked=False, in_place=False)

    assert row["context"] == "the paragraph it was read from"


def test_suboperation_row_carries_an_empty_context_when_there_is_none() -> None:
    row = view.suboperation_row(_sub(1), checked=False, in_place=False)

    assert row["context"] == ""


def test_sub_values_keep_current_whole_and_clip_the_one_line_proposal() -> None:
    """The current cell may grow; the proposal stays one line with a full tooltip."""
    long_current = "y" * 300
    long_proposed = "x" * 300
    sub = _sub(1, proposed=long_proposed, current=long_current)

    current_full, proposed_full = view.sub_values_full(sub)
    current, proposed = view.sub_values(sub)

    assert current_full == long_current
    assert current == long_current
    assert proposed_full == long_proposed
    assert len(proposed) == 121
    assert proposed.endswith("…")


def test_recommendation_text_prefers_the_most_substantial_proposed_value() -> None:
    """The collapsed row shows the content worth judging, not the short title."""
    subs = [
        {"label": "Title", "proposed": "124.76", "proposed_full": "124.76"},
        {
            "label": "Rec",
            "proposed": "Ensure an open and safe civic space…",
            "proposed_full": "Ensure an open and safe civic space by guaranteeing freedom of speech.",
        },
    ]

    assert view.recommendation_text(subs) == "Ensure an open and safe civic space by guaranteeing freedom of speech."


def test_recommendation_text_falls_back_to_the_first_label_without_a_proposal() -> None:
    assert view.recommendation_text([{"label": "Delete", "proposed": "—"}]) == "Delete"
    assert view.recommendation_text([{"label": "Copied file", "proposed": "a.pdf", "structural": True}]) == "Copied file"
