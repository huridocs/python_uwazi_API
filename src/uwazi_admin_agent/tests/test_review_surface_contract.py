"""Isolated unit tests for the review surfaces' new contracts (per AGENTS.md).

Pure helpers only: no network, no mocks, no NiceGUI. They pin the behaviors the
operator asked for after reviewing real runs:

* the Target cell drops the sharedId and the title itself carries the Uwazi link;
* the Status badge is hidden when it would only repeat the operation;
* a row whose parts disagree says ``mixed`` and names the split;
* a proposed multi-value set is the WHOLE set (no duplicate "will write" line);
* the entity's own property values become the row's evidence text.
"""

from __future__ import annotations

from datetime import datetime, timezone

from uwazi_admin_agent.domain.property_evidence import basis_context_text, basis_values
from uwazi_admin_agent.domain.suggestion import OperationReviewRow, Suggestion, SuggestionStatus
from uwazi_admin_agent.drivers.web import operations_view as view


def _sub(**kwargs: object) -> Suggestion:
    base: dict[str, object] = {
        "id": 1,
        "run_id": "r",
        "shared_id": "ent",
        "entity_title": "Title",
        "op": "update",
        "property_name": "states",
        "language": None,
        "proposed_value": [{"value": "a"}],
        "status": SuggestionStatus.PENDING,
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }
    base.update(kwargs)
    return Suggestion(**base)  # type: ignore[arg-type]


def _row(subs: list[Suggestion], *, ops: list[str] | None = None) -> OperationReviewRow:
    return OperationReviewRow(
        shared_id="ent",
        entity_title="Title",
        template_names=["Rec"],
        ops=ops or ["update"],
        properties=["states"],
        languages=[],
        proposal_count=len(subs),
        pending=sum(1 for s in subs if s.status is SuggestionStatus.PENDING),
        applied=sum(1 for s in subs if s.status is SuggestionStatus.APPLIED),
        reverted=0,
        discarded=0,
        match_summary="n/a",
        has_outcome=True,
        subs=subs,
    )


# --- Target cell ----------------------------------------------------------------


def test_the_target_cell_no_longer_prints_the_shared_id() -> None:
    """The title IS the target; the sharedId is gone from the row contract."""
    payload = view.operation_row(_row([_sub()]))
    assert "display_shared_id" not in payload
    assert payload["url"] is None  # no instance configured in this test
    assert payload["title"] == "Title"


def test_a_created_entity_links_through_its_title_once_it_exists() -> None:
    """A create row's title carries the link to the entity a Run minted."""
    applied = _sub(op="create", status=SuggestionStatus.APPLIED, created_shared_id="REAL1")
    payload = view.operation_row(_row([applied], ops=["create"]), uwazi_url="http://instance")

    assert payload["url"] == "http://instance/en/entity/REAL1"
    assert "display_shared_id" not in payload


# --- Status column -------------------------------------------------------------


def test_a_display_only_row_does_not_repeat_its_operation_as_a_status() -> None:
    skipped = _sub(op="skipped", property_name=None, status=SuggestionStatus.PENDING)
    payload = view.operation_row(_row([skipped], ops=["skipped"]))

    assert payload["status_hidden"] is True
    assert payload["run_status"] == "excluded"


def test_a_normal_row_keeps_its_status_badge() -> None:
    assert view.operation_row(_row([_sub()]))["status_hidden"] is False


def test_parts_that_disagree_read_mixed_and_name_the_split() -> None:
    """The main-row status must answer 'which status is this row?' honestly."""
    applied = _sub(id=1, status=SuggestionStatus.APPLIED)
    pending = _sub(id=2, status=SuggestionStatus.PENDING)
    payload = view.operation_row(_row([applied, pending]))

    assert payload["run_status"] == "mixed"
    assert payload["run_status_color"] == "deep-orange-9"
    assert payload["status_breakdown"] == "1 applied · 1 pending"


def test_parts_that_agree_carry_no_breakdown() -> None:
    payload = view.operation_row(_row([_sub(id=1), _sub(id=2)]))
    assert payload["run_status"] == "pending"
    assert payload["status_breakdown"] == ""


def test_operations_and_statuses_use_disjoint_colors() -> None:
    """An operation badge and a status badge must never read as the same color."""
    assert view.OP_COLORS["update"] != view.RUN_STATUS_COLORS["applied"]
    assert view.OP_COLORS["delete"] != view.RUN_STATUS_COLORS["discarded"]
    assert view.OP_COLORS["publish"] != view.RUN_STATUS_COLORS["applied"]


# --- Proposed value ------------------------------------------------------------


def test_a_created_entity_is_revertable_from_its_row() -> None:
    """Reverting a create DELETES the minted entity: the row offers the undo."""
    applied = _sub(op="create", status=SuggestionStatus.APPLIED, created_shared_id="REAL1")
    payload = view.operation_row(_row([applied], ops=["create"]), uwazi_url="http://instance")

    assert payload["in_place"] is True
    assert payload["revertable"] is True


def test_a_delete_row_is_still_run_level_only() -> None:
    """A delete's undo re-creates the entity with a new id — not an in-place undo."""
    deleted = _sub(op="delete", property_name=None, status=SuggestionStatus.APPLIED)
    payload = view.operation_row(_row([deleted], ops=["delete"]))

    assert payload["revertable"] is False


def test_a_multivalue_proposal_renders_one_chip_per_value_and_no_repeat_line() -> None:
    sub = _sub(proposed_value=[{"value": "a"}, {"value": "b"}])
    payload = view.suboperation_row(
        sub, checked=False, in_place=True, types={"states": "relationship"}, titles={"a": "A", "b": "B"}
    )

    assert [chip["label"] for chip in payload["values"]] == ["A", "B"]
    # The "Related entities to write: …" line is gone from the table; the chips
    # already say it, so nothing repeats the same set.
    assert payload["extra_selection"] == ""


# --- evidence context --------------------------------------------------------------


def test_basis_values_reads_the_entities_own_properties() -> None:
    raw = {"title": "T", "metadata": {"document_ref": [{"value": "doc-9"}]}}
    assert basis_values(raw, ["_title", "document_ref"]) == {
        "_title": "T",
        "document_ref": [{"value": "doc-9"}],
    }


def test_basis_context_text_names_the_property_and_its_value() -> None:
    raw = {"title": "T", "metadata": {"document_ref": [{"value": "doc-9"}]}}
    assert basis_context_text(raw, ["_title", "document_ref"], {"document_ref": "Document Ref"}) == (
        "Title: T\nDocument Ref: doc-9"
    )


def test_basis_context_text_is_empty_without_evidence() -> None:
    assert basis_context_text(None, ["_title"]) == ""
    assert basis_context_text({"title": "T"}, []) == ""
    assert basis_context_text({"title": "T"}, ["absent"]) == ""


def test_every_real_operation_hue_is_distinct_from_every_workflow_status_hue() -> None:
    """The two adjacent columns must never paint the same word in the same color.

    ``matched``/``skipped``/``info`` are skipped: those ARE display outcomes, so
    they are painted like the status they report on purpose.
    """
    from uwazi_admin_agent.drivers.web.review_view import OP_COLORS, RUN_STATUS_COLORS

    workflow = {
        "pending": RUN_STATUS_COLORS["pending"],
        "applied": RUN_STATUS_COLORS["applied"],
        "reverted": RUN_STATUS_COLORS["reverted"],
        "discarded": RUN_STATUS_COLORS["discarded"],
        "mixed": RUN_STATUS_COLORS["mixed"],
    }
    display_ops = {"info", "matched", "skipped"}
    real_ops = {name: color for name, color in OP_COLORS.items() if name not in display_ops}
    assert set(real_ops.values()).isdisjoint(workflow.values())
