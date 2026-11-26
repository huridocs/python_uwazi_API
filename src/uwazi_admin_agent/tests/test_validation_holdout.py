"""Isolated unit tests for the holdout enforcement (per AGENTS.md).

The feature's whole value is the invariant: a validation entity is invisible to
the authoring side, never selectable, never pushed by a Run, and protected from
the production reconcile. These tests pin those rules with real objects — a
small in-file entity API, no mocks — as the AGENTS.md unit tier requires.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from uwazi_admin_agent.adapters.sqlite_suggestion_store import SqliteSuggestionStore
from uwazi_admin_agent.adapters.validation_holdout import (
    ValidationExcludingEntityApi,
    ValidationExcludingEntityRepository,
    ValidationHoldoutError,
    drop_excluded,
)
from uwazi_admin_agent.domain.suggestion import Suggestion, SuggestionFilter, SuggestionStatus
from uwazi_admin_agent.drivers.web.operations_view import suboperation_row
from uwazi_admin_agent.drivers.web.review_page.core import holdout_warning_message
from uwazi_admin_agent.use_cases.apply_suggestions_use_case.payloads import pushable_rows
from uwazi_agent.domain.agent_entity import AgentEntity
from uwazi_agent.domain.agent_entity_search_result import AgentEntitySearchResult
from uwazi_agent.domain.agent_entity_summary import AgentEntitySummary


def _entity(shared_id: str, title: str = "") -> AgentEntity:
    return AgentEntity(shared_id=shared_id, title=title or shared_id, template_name="T")


def _result(entities: list[AgentEntity]) -> AgentEntitySearchResult:
    """A search result shaped exactly like the API adapter's (examples + private list)."""
    result = AgentEntitySearchResult(
        summary=AgentEntitySummary(
            count=len(entities),
            by_template={"T": len(entities)} if entities else {},
            sample_titles=[entity.title or "" for entity in entities[:5]],
            shared_ids=[entity.shared_id for entity in entities],
        ),
        examples=entities[:5],
    )
    result._all_entities = entities
    return result


class _RecordingEntityApi:
    """A real (tiny) EntityApiPort: records what it was asked and answers from a fixed set."""

    def __init__(self, entities: list[AgentEntity]) -> None:
        self._entities = entities
        self.calls: list[tuple[str, Any]] = []

    async def get_entities_by_shared_ids(self, shared_ids, language, limit=10000):
        self.calls.append(("by_ids", list(shared_ids)))
        return [entity for entity in self._entities if entity.shared_id in set(shared_ids)]

    async def search_entities_by_text(self, search_term, template_name, language, limit):
        self.calls.append(("by_text", search_term))
        return _result(self._entities)

    async def get_entities_by_template(self, template_name, language, limit):
        self.calls.append(("by_template", template_name))
        return _result(self._entities)

    async def search_entities_by_filter(self, template_name, filters, language, limit, published=None):
        self.calls.append(("by_filter", template_name))
        return _result(self._entities)

    async def create_entities(self, entities, language):
        return []

    async def update_entities(self, updates, language):
        return []

    async def delete_entities_by_shared_ids(self, shared_ids):
        return []

    async def set_entities_publish_status(self, shared_ids, published):
        return []

    async def get_publish_status(self, shared_ids, language):
        self.calls.append(("publish_status", list(shared_ids)))
        return []


def test_drop_excluded_removes_both_the_private_list_and_the_summary() -> None:
    """The summary's count and sample ids are a leak surface of their own."""
    kept = _result([_entity("a"), _entity("b"), _entity("c")])
    filtered = drop_excluded(kept, frozenset({"b"}))
    assert [entity.shared_id for entity in filtered._all_entities] == ["a", "c"]
    assert filtered.summary.count == 2
    assert filtered.summary.shared_ids == ["a", "c"]
    assert "b" not in filtered.summary.sample_titles


def test_a_template_scan_never_returns_a_held_out_entity() -> None:
    api = ValidationExcludingEntityApi(_RecordingEntityApi([_entity("a"), _entity("b")]), {"b"})
    import asyncio

    result = asyncio.run(api.get_entities_by_template("T", "en", 100))
    assert [entity.shared_id for entity in result._all_entities] == ["a"]


def test_by_ids_drops_the_held_out_entities_and_can_return_empty() -> None:
    import asyncio

    inner = _RecordingEntityApi([_entity("a"), _entity("b")])
    api = ValidationExcludingEntityApi(inner, {"b"})
    assert [entity.shared_id for entity in asyncio.run(api.get_entities_by_shared_ids(["a", "b"], "en"))] == ["a"]
    assert asyncio.run(api.get_entities_by_shared_ids(["b"], "en")) == []
    # The excluded id never reaches the instance: the inner call got only "a"
    # (and nothing at all for the second).
    assert inner.calls == [("by_ids", ["a"])]


class _RecordingRawRepository:
    """A real (tiny) EntityRepositoryPort: the read methods record and answer."""

    def __init__(self) -> None:
        self.read_ids: list[str] = []

    async def get_raw_by_shared_id(self, shared_id, language=None):
        self.read_ids.append(shared_id)
        return {"sharedId": shared_id, "metadata": {}}

    async def get_raw_by_internal_id(self, internal_id):
        return {"_id": internal_id}

    async def save_raw(self, raw):
        return None

    async def save_raw_properties(self, payload):
        return None

    async def update_many_properties(self, shared_ids, metadata, language):
        return list(shared_ids)

    async def create_raw(self, raw):
        return "new-id"

    async def delete_by_shared_id(self, shared_id):
        return None


def test_a_raw_read_of_a_held_out_entity_is_refused_loudly() -> None:
    """An empty answer would look like "no files" and mislead the extractor author."""
    import asyncio

    import pytest

    inner = _RecordingRawRepository()
    repository = ValidationExcludingEntityRepository(inner, {"b"})
    with pytest.raises(ValidationHoldoutError):
        asyncio.run(repository.get_raw_by_shared_id("b", "en"))
    assert inner.read_ids == []
    assert asyncio.run(repository.get_raw_by_shared_id("a", "en"))["sharedId"] == "a"


def _sub(*, is_validation: bool, status: SuggestionStatus = SuggestionStatus.PENDING) -> Suggestion:
    return Suggestion(
        id=1,
        run_id="run-1",
        shared_id="a",
        op="update",
        property_name="document_ref",
        status=status,
        proposed_value="X",
        current_value="Y",
        is_validation=is_validation,
    )


def test_a_run_never_pushes_a_validation_row() -> None:
    """The guard lives in the apply payload builder, not only in the view."""
    accepted = _sub(is_validation=True, status=SuggestionStatus.PENDING)
    regular = _sub(is_validation=False, status=SuggestionStatus.PENDING)
    assert pushable_rows([accepted, regular]) == [regular]


def test_the_validation_filter_is_usable_by_the_store() -> None:
    assert SuggestionFilter(is_validation=True).is_validation is True
    assert SuggestionFilter().is_validation is None


def test_the_table_reads_a_validation_row_as_validate_and_never_runnable() -> None:
    payload = suboperation_row(_sub(is_validation=True), checked=False, in_place=True)
    assert payload["status"] == "validate"
    assert payload["runnable"] is False
    assert payload["selectable"] is False
    assert "validation row" in payload["note"]
    assert payload["op"] == "update"


def test_a_regular_row_of_the_same_shape_keeps_its_normal_payload() -> None:
    payload = suboperation_row(_sub(is_validation=False), checked=False, in_place=True)
    assert payload["status"] == "pending"
    assert payload["runnable"] is True


def test_the_run_confirmation_warns_about_the_holdout() -> None:
    message = holdout_warning_message(2)
    assert "2 entities" in message
    assert "validation set" in message
    assert "fair estimate" in message
    assert "1 entity " in holdout_warning_message(1)


def _row(shared_id: str, *, is_validation: bool, status: SuggestionStatus = SuggestionStatus.PENDING) -> Suggestion:
    return Suggestion(
        id=None,
        run_id="run-1",
        shared_id=shared_id,
        op="update",
        property_name="document_ref",
        status=status,
        proposed_value="X",
        current_value="Y",
        is_validation=is_validation,
    )


def test_the_store_keeps_holdout_rows_out_of_the_work_totals(tmp_path: Path) -> None:
    """The header counts describe WORK; validation rows are evidence and get their own card."""
    store = SqliteSuggestionStore(tmp_path / "s.db")
    store.add_suggestions(
        [
            _row("a", is_validation=False, status=SuggestionStatus.PENDING),
            _row("b", is_validation=True),
        ]
    )
    counts = store.counts("run-1")
    assert counts["operations_total"] == 1
    assert counts["subs_total"] == 1
    assert (counts["validation_rows"], counts["validation_entities"]) == (1, 1)
    # The selection projection a Run reads never includes the holdout row.
    assert [ref[1] for ref in store.selection_rows("run-1", SuggestionFilter(), runnable_only=True)] == ["a"]


def test_a_production_reconcile_never_deletes_a_validation_row(tmp_path: Path) -> None:
    """The production pass excludes the holdout, so it cannot retire its evidence."""
    store = SqliteSuggestionStore(tmp_path / "s.db")
    store.add_suggestions([_row("b", is_validation=True)])
    summary = store.reconcile_suggestions("run-1", [])
    assert summary.removed == 0
    rows, total = store.query("run-1", SuggestionFilter(is_validation=True))
    assert total == 1 and rows[0].shared_id == "b"


def test_promotion_turns_the_entity_into_regular_pending_work(tmp_path: Path) -> None:
    store = SqliteSuggestionStore(tmp_path / "s.db")
    store.add_suggestions([_row("b", is_validation=True)])
    assert store.promote_to_regular("run-1", "b") == 1
    rows, _total = store.query("run-1", SuggestionFilter(shared_ids=["b"]))
    assert rows[0].is_validation is False
    assert rows[0].status is SuggestionStatus.PENDING
    assert [ref[1] for ref in store.selection_rows("run-1", SuggestionFilter(), runnable_only=True)] == ["b"]


def test_a_new_script_version_replaces_the_validation_rows_only(tmp_path: Path) -> None:
    """A regeneration must not accumulate evidence from a script that no longer exists."""
    store = SqliteSuggestionStore(tmp_path / "s.db")
    store.add_suggestions([_row("a", is_validation=False, status=SuggestionStatus.PENDING)])
    store.replace_validation_rows(
        "run-1",
        [_row("b", is_validation=True), _row("c", is_validation=True)],
    )
    store.replace_validation_rows("run-1", [_row("b", is_validation=True)])
    rows, total = store.query("run-1", SuggestionFilter(is_validation=True))
    assert total == 1 and rows[0].shared_id == "b"
    # The regular row (and its status) is untouched by the validation write path.
    assert store.query("run-1", SuggestionFilter(is_validation=False))[1] == 1


def test_a_validation_row_shows_the_frozen_label_as_its_current_value() -> None:
    """The operator must see Uwazi's value beside the suggestion to judge it.

    The evaluation pass records only what the script proposed; the score is
    measured against the label frozen with the set, so that label is the row's
    before-state.
    """
    from uwazi_admin_agent.domain.suggestion import Suggestion
    from uwazi_admin_agent.domain.validation import ValidationSet
    from uwazi_admin_agent.use_cases.evaluate_validation_use_case import _with_frozen_label

    validation = ValidationSet(
        run_id="r",
        property_name="document_ref",
        property_type="text",
        shared_ids=["a"],
        labels={"a|en": [{"value": "A/HRC/37/2"}]},
        created_at=__import__("datetime").datetime(2026, 1, 1),
    )
    row = Suggestion(
        run_id="r",
        shared_id="a",
        op="update",
        property_name="document_ref",
        language="en",
        proposed_value=[{"value": "A/HRC/37/2"}],
    )

    enriched = _with_frozen_label(row, validation)

    assert enriched.is_validation is True
    assert enriched.current_value == [{"value": "A/HRC/37/2"}]
    assert enriched.match is True


def test_a_validation_row_whose_label_differs_reads_as_a_mismatch() -> None:
    from datetime import datetime

    from uwazi_admin_agent.domain.suggestion import Suggestion
    from uwazi_admin_agent.domain.validation import ValidationSet
    from uwazi_admin_agent.use_cases.evaluate_validation_use_case import _with_frozen_label

    validation = ValidationSet(
        run_id="r",
        property_name="document_ref",
        property_type="text",
        shared_ids=["a"],
        labels={"a|en": [{"value": "A/HRC/37/2"}]},
        created_at=datetime(2026, 1, 1),
    )
    row = Suggestion(
        run_id="r",
        shared_id="a",
        op="update",
        property_name="document_ref",
        language="en",
        proposed_value=[{"value": "A/HRC/37/9"}],
    )

    assert _with_frozen_label(row, validation).match is False


def test_a_relationship_validation_row_reads_the_shared_frozen_label() -> None:
    """A relationship has ONE value per entity, so the row carries no language.

    The set freezes its label under the language it was sampled in. Looking the
    label up by the row's own key alone left every relationship validation row
    with no current value at all — the reported "the validation rows have no
    suggestions".
    """
    from datetime import datetime

    from uwazi_admin_agent.domain.suggestion import Suggestion
    from uwazi_admin_agent.domain.validation import ValidationSet
    from uwazi_admin_agent.use_cases.evaluate_validation_use_case import _with_frozen_label

    validation = ValidationSet(
        run_id="r",
        property_name="states",
        property_type="relationship",
        shared_ids=["a"],
        labels={"a|en": [{"shared_id": "id-1", "title": "Japan"}]},
        created_at=datetime(2026, 1, 1),
    )
    row = Suggestion(run_id="r", shared_id="a", op="update", property_name="states", proposed_value=["id-1"])

    enriched = _with_frozen_label(row, validation)

    assert enriched.current_value == [{"shared_id": "id-1", "title": "Japan"}]
    assert enriched.match is True, "a set-valued property compares ids, not rendered text"


def test_a_property_type_that_is_per_language_never_borrows_another_language() -> None:
    """A translation row's label is the value of ITS language, never a sibling's."""
    from datetime import datetime

    from uwazi_admin_agent.domain.suggestion import Suggestion
    from uwazi_admin_agent.domain.validation import ValidationSet
    from uwazi_admin_agent.use_cases.evaluate_validation_use_case import _with_frozen_label

    validation = ValidationSet(
        run_id="r",
        property_name="summary",
        property_type="text",
        shared_ids=["a"],
        labels={"a|es": [{"value": "Resumen"}]},
        created_at=datetime(2026, 1, 1),
    )
    row = Suggestion(
        run_id="r", shared_id="a", op="update", property_name="summary", language="en", proposed_value="Summary"
    )

    assert _with_frozen_label(row, validation).current_value is None


def test_a_held_out_entity_the_script_skipped_gets_a_measured_row() -> None:
    """The skipped row must read "validate" with the frozen label beside it.

    The script's record is a `skipped` record, which the review table renders as
    display-only; under the validation set the row is evidence for the score, so
    it shows the measured property, the frozen label, and the script's own reason
    for proposing nothing — never the word "excluded".
    """
    from datetime import datetime

    from uwazi_admin_agent.domain.suggestion import OperationReviewRow
    from uwazi_admin_agent.domain.validation import ValidationSet
    from uwazi_admin_agent.drivers.web import operations_view as view
    from uwazi_admin_agent.use_cases.evaluate_validation_use_case import _missing_rows

    validation = ValidationSet(
        run_id="r",
        property_name="states",
        property_type="relationship",
        shared_ids=["a"],
        labels={"a|en": [{"shared_id": "id-1", "title": "Japan"}]},
        created_at=datetime(2026, 1, 1),
    )
    rows = _missing_rows([], validation, {"a": "no STATE entity matched (Japan)"})
    assert len(rows) == 1
    skipped_row = rows[0]
    assert skipped_row.property_name == "states"
    assert skipped_row.current_value == [{"shared_id": "id-1", "title": "Japan"}]
    assert skipped_row.context == "no STATE entity matched (Japan)"
    assert skipped_row.is_validation is True

    operation = OperationReviewRow(
        shared_id="a",
        entity_title="A title",
        template_names=["Rec"],
        ops=["update"],
        properties=["states"],
        proposal_count=1,
        pending=1,
        applied=0,
        reverted=0,
        discarded=0,
        validation=1,
        match_summary="n/a",
        has_outcome=True,
        subs=[skipped_row],
    )
    payload = view.operation_row(operation)
    assert payload["ops"] == "validate"
    # Evidence, never work: no checkbox, no Run/Discard/Revert action.
    assert payload["is_display_only"] is True
    assert payload["has_checkable"] is False
    assert payload["has_runnable"] is False
    sub = payload["subs"][0]
    assert sub["status"] == "validate"
    assert sub["label"] == "states"
    assert "Japan" in sub["current"]


def test_a_skipped_holdout_record_is_rendered_as_the_measured_row() -> None:
    """The recorded `skipped` op is a missing prediction, not an "excluded" outcome."""
    from uwazi_admin_agent.domain.suggestion import OperationReviewRow, Suggestion
    from uwazi_admin_agent.drivers.web import operations_view as view

    row = Suggestion(
        id=7,
        run_id="r",
        shared_id="a",
        op="skipped",
        status=SuggestionStatus.PENDING,
        is_validation=True,
        skip_reason="no_code_in_page_1",
        proposed_value="no STATE entity matched (Japan)",
    )
    operation = OperationReviewRow(
        shared_id="a",
        entity_title="A title",
        template_names=["Rec"],
        ops=["skipped"],
        properties=[],
        proposal_count=1,
        pending=1,
        applied=0,
        reverted=0,
        discarded=0,
        validation=1,
        match_summary="n/a",
        has_outcome=False,
        subs=[row],
    )
    payload = view.operation_row(operation)
    assert payload["ops"] == "validate"
    assert payload["run_status"] == "validate"
