"""End-to-end test of the prediction pass with real in-memory ports (per AGENTS.md).

Real implementations only: an in-memory :class:`EntityApiPort`, an in-memory
:class:`EntityRepositoryPort`, a real on-``tmp_path`` SQLite store, and an
in-memory :class:`LlmRuntimePort` whose calls are pure functions. No mocks, no
monkeypatching, no network, no instance.

This is the pass's own verification: a script runs as a prediction, its recorded
writes become hydrated suggestions, a subset restricts what it sees, and a
budget breach aborts cleanly with the earlier suggestions kept.
"""

import asyncio
from typing import Any

import pytest

from uwazi_admin_agent.adapters.sqlite_suggestion_store import SqliteSuggestionStore
from uwazi_admin_agent.domain.llm_budget import LlmBudget, LlmLedger, LlmPrice, LlmUsage
from uwazi_admin_agent.domain.suggestion import PredictionAttemptStatus, Suggestion, SuggestionFilter, SuggestionStatus
from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort
from uwazi_admin_agent.ports.llm_runtime_port import LlmBatchResult, LlmCallResult, LlmRuntimePort
from uwazi_admin_agent.use_cases.generate_predictions_use_case import (
    GeneratePredictionsUseCase,
    _apply_hydration,
    _extract_property,
)
from uwazi_agent.domain.agent_entity import AgentEntity
from uwazi_agent.domain.agent_entity_create import AgentEntityCreate
from uwazi_agent.domain.agent_entity_mutation_result import AgentEntityMutationResult
from uwazi_agent.domain.agent_entity_search_result import AgentEntitySearchResult
from uwazi_agent.domain.agent_entity_summary import AgentEntitySummary
from uwazi_agent.domain.agent_publish_status import AgentPublishStatus
from uwazi_agent.domain.agent_search_filter import AgentSearchFilter
from uwazi_agent.ports.entity_api_port import EntityApiPort

PRICE = LlmPrice(model="test", input_per_mtok=0.001, output_per_mtok=0.002)


class InMemoryEntityApi(EntityApiPort):
    """Real in-memory instance: template search + by_ids reads, no I/O."""

    def __init__(self, entities: list[AgentEntity]) -> None:
        self._entities = list(entities)

    async def create_entities(self, entities: list[AgentEntityCreate], language: str) -> list[AgentEntityMutationResult]:
        return [AgentEntityMutationResult(success=True, shared_id=f"new-{i}") for i, _e in enumerate(entities)]

    async def get_entities_by_shared_ids(
        self, shared_ids: list[str], language: str, limit: int = 10000
    ) -> list[AgentEntity]:
        wanted = set(shared_ids[:limit])
        return [e for e in self._entities if e.shared_id in wanted]

    async def search_entities_by_text(
        self, search_term: str, template_name: str | None, language: str, limit: int
    ) -> AgentEntitySearchResult:
        return self._result([e for e in self._entities if search_term in (e.title or "")], limit)

    async def get_entities_by_template(self, template_name: str, language: str, limit: int) -> AgentEntitySearchResult:
        return self._result([e for e in self._entities if e.template_name == template_name], limit)

    async def search_entities_by_filter(
        self,
        template_name: str,
        filters: list[AgentSearchFilter],
        language: str,
        limit: int,
        published: bool | None = None,
    ) -> AgentEntitySearchResult:
        return self._result([e for e in self._entities if e.template_name == template_name], limit)

    async def update_entities(self, updates: list[AgentEntity], language: str) -> list[AgentEntityMutationResult]:
        return [AgentEntityMutationResult(success=True, shared_id=u.shared_id) for u in updates]

    async def delete_entities_by_shared_ids(self, shared_ids: list[str]) -> list[AgentEntityMutationResult]:
        return [AgentEntityMutationResult(success=True, shared_id=sid) for sid in shared_ids]

    async def set_entities_publish_status(self, shared_ids: list[str], published: bool) -> list[AgentEntityMutationResult]:
        return [AgentEntityMutationResult(success=True, shared_id=sid) for sid in shared_ids]

    async def get_publish_status(self, shared_ids: list[str], language: str) -> list[AgentPublishStatus]:
        return [AgentPublishStatus(shared_id=sid, published=True) for sid in shared_ids]

    def _result(self, matched: list[AgentEntity], limit: int) -> AgentEntitySearchResult:
        capped = matched[:limit]
        result = AgentEntitySearchResult(
            summary=AgentEntitySummary(
                count=len(capped),
                shared_ids=[e.shared_id for e in capped],
                sample_titles=[e.title or "" for e in capped[:3]],
            ),
            examples=capped[:3],
        )
        result._all_entities = capped
        return result


class InMemoryEntityRepository(EntityRepositoryPort):
    """Real in-memory raw repository keyed by sharedId (no I/O)."""

    def __init__(self, raws: dict[str, dict[str, Any]]) -> None:
        self._raws = dict(raws)
        self.saved: list[dict[str, Any]] = []

    async def get_raw_by_shared_id(self, shared_id: str, language: str | None = None) -> dict[str, Any]:
        if shared_id not in self._raws:
            raise FileNotFoundError(shared_id)
        return dict(self._raws[shared_id])

    async def get_raw_by_internal_id(self, internal_id: str) -> dict[str, Any]:
        for raw in self._raws.values():
            if raw.get("_id") == internal_id:
                return dict(raw)
        raise FileNotFoundError(internal_id)

    async def save_raw(self, raw: dict[str, Any]) -> None:
        self.saved.append(raw)

    async def create_raw(self, raw: dict[str, Any]) -> str:
        shared_id = f"restored-{len(self.saved)}"
        self._raws[shared_id] = dict(raw)
        return shared_id

    async def delete_by_shared_id(self, shared_id: str) -> None:
        self._raws.pop(shared_id, None)


class RecordingRuntime(LlmRuntimePort):
    """Real runtime: pure transforms, counted calls, optional batch failure."""

    def __init__(self, calls_before_error: int | None = None) -> None:
        self.calls = 0
        self._calls_before_error = calls_before_error

    async def complete(self, instruction: str, text: str) -> LlmCallResult:
        self.calls += 1
        return LlmCallResult(output=f"{instruction}:{text}", usage=LlmUsage(requests=1, input_tokens=4, output_tokens=2))

    async def map_batch(self, instruction: str, items: list[str]) -> LlmBatchResult:
        self.calls += 1
        return LlmBatchResult(
            outputs=[f"{instruction}:{item}" for item in items],
            usage=LlmUsage(requests=1, input_tokens=len(items), output_tokens=len(items)),
        )


def _budget(requests: int = 100, cost: float = 1_000.0) -> LlmBudget:
    return LlmBudget(max_cost=cost, max_requests=requests, price=PRICE)


def _entities() -> list[AgentEntity]:
    return [
        AgentEntity(shared_id="s1", title="One", template_name="Case", metadata={"excerpt": [{"value": "old"}]}),
        AgentEntity(shared_id="s2", title="Two", template_name="Case", metadata={"excerpt": [{"value": "same"}]}),
        AgentEntity(shared_id="s3", title="Three", template_name="Case", metadata={}),
    ]


def _raws() -> dict[str, dict[str, Any]]:
    return {
        "s1": {"sharedId": "s1", "title": "One", "metadata": {"excerpt": [{"value": "old"}]}},
        "s2": {"sharedId": "s2", "title": "Two", "metadata": {"excerpt": [{"value": "same"}]}},
        "s3": {"sharedId": "s3", "title": "Three", "metadata": {}},
    }


@pytest.fixture()
def store(tmp_path) -> SqliteSuggestionStore:
    db = SqliteSuggestionStore(tmp_path / "suggestions.db")
    yield db
    db.close()


def _use_case(
    store: SqliteSuggestionStore,
    runtime: LlmRuntimePort | None = None,
    api: EntityApiPort | None = None,
    repository: EntityRepositoryPort | None = None,
) -> GeneratePredictionsUseCase:
    # ``runtime`` is kept in the signature for call-site compatibility but the
    # SELECTION pass never receives an LlmRuntimePort: the contract binds
    # llm-helper REFUSALS, so an LLM-calling selection script fails loudly.
    del runtime
    return GeneratePredictionsUseCase(
        entity_api=api or InMemoryEntityApi(_entities()),
        entity_repository=repository or InMemoryEntityRepository(_raws()),
        file_repository=None,
        segmentation_repository=None,
        url_fetcher=None,
        suggestion_store=store,
    )


# --- _extract_property -------------------------------------------------------


def test_extract_property_reads_title_and_metadata() -> None:
    raw = {"title": "T", "metadata": {"a": [{"value": "x"}]}}
    assert _extract_property(raw, "_title") == "T"
    assert _extract_property(raw, "a") == [{"value": "x"}]
    assert _extract_property(raw, "missing") is None
    assert _extract_property({"metadata": "not a dict"}, "a") is None


def test_hydration_reads_each_rows_current_value_from_its_own_language() -> None:
    suggestions = [
        Suggestion(
            run_id="r",
            shared_id="s1",
            op="update",
            property_name="excerpt",
            language="es",
            proposed_value=[{"value": "nuevo"}],
        ),
        Suggestion(
            run_id="r",
            shared_id="s1",
            op="update",
            property_name="excerpt",
            language="en",
            proposed_value=[{"value": "new"}],
        ),
        Suggestion(
            run_id="r",
            shared_id="s1",
            op="update",
            property_name="case_status",
            proposed_value="Closed",
        ),
    ]
    raws = {
        ("s1", "es"): {"sharedId": "s1", "language": "es", "title": "T es", "metadata": {"excerpt": [{"value": "viejo"}]}},
        ("s1", "en"): {"sharedId": "s1", "language": "en", "title": "T en", "metadata": {"excerpt": [{"value": "old"}]}},
    }

    _apply_hydration(suggestions, raws, default_language="en")

    assert suggestions[0].current_value == [{"value": "viejo"}]
    assert suggestions[0].match is False
    assert suggestions[1].current_value == [{"value": "old"}]
    assert suggestions[1].match is False


def test_hydration_does_not_use_a_fallback_row_of_another_language() -> None:
    suggestions = [
        Suggestion(
            run_id="r",
            shared_id="s1",
            op="update",
            property_name="excerpt",
            language="es",
            proposed_value=[{"value": "nuevo"}],
        )
    ]
    # Uwazi answered the missing `es` row with the default `en` one.
    raws = {
        ("s1", "es"): {"sharedId": "s1", "language": "en", "title": "T en", "metadata": {"excerpt": [{"value": "english"}]}},
    }

    _apply_hydration(suggestions, raws, default_language="en")

    assert suggestions[0].current_value is None
    assert suggestions[0].match is None


# --- whole-instance pass -----------------------------------------------------


def test_prediction_pass_persists_suggestions_with_accuracy(store: SqliteSuggestionStore) -> None:
    """A script's would-be writes become hydrated rows with real match verdicts."""
    script = """
entities = query_entities_full(mode="by_template", template_name="Case")
for d in entities:
    update_entities([{"shared_id": d["shared_id"], "metadata": {"excerpt": [{"value": "new"}]}}])
result = len(entities)
"""
    pass_row = asyncio.run(_use_case(store).run(script, "run1", _budget()))
    assert pass_row.status == PredictionAttemptStatus.COMPLETED
    rows, total = store.query("run1", SuggestionFilter())
    assert total == 3
    by_id = {row.shared_id: row for row in rows}
    # s1 proposed "new" vs current "old" -> mismatch; s2 proposed "new" vs "same" -> mismatch;
    # s3 had no excerpt -> new value (no verdict).
    assert by_id["s1"].match is False
    assert by_id["s1"].current_value == [{"value": "old"}]
    assert by_id["s2"].match is False
    assert by_id["s3"].match is None
    counts = store.counts("run1")
    assert counts["match_mismatch"] == 2
    assert counts["match_new"] == 1


def test_prediction_pass_drops_exact_matches(store: SqliteSuggestionStore) -> None:
    """A row that proposes the value the entity already has is not a change.

    Hydration marks it a match, then the pass drops it: the review must not
    offer a "value → same value" suboperation, and a Run must not re-send a
    pointless write. A row with no current value (s3) is a genuine new value
    and survives.
    """
    script = """
for d in query_entities_full(mode="by_template", template_name="Case"):
    update_entities([{"shared_id": d["shared_id"], "metadata": {"excerpt": d["metadata"].get("excerpt")}}])
result = "ok"
"""
    asyncio.run(_use_case(store).run(script, "run1", _budget()))
    rows, _ = store.query("run1", SuggestionFilter())
    assert [(row.shared_id, row.match) for row in rows] == [("s3", None)]


def test_prediction_pass_records_progress_and_finish(store: SqliteSuggestionStore) -> None:
    script = """
for d in query_entities_full(mode="by_template", template_name="Case"):
    update_entities([{"shared_id": d["shared_id"], "metadata": {"x": 1}}])
result = "ok"
"""
    asyncio.run(_use_case(store).run(script, "run1", _budget()))
    pass_row = store.get_pass("run1")
    assert pass_row is not None
    assert pass_row.status == PredictionAttemptStatus.COMPLETED
    assert pass_row.entities_total == 3
    assert pass_row.entities_done == 3
    assert pass_row.finished_at is not None
    assert pass_row.entity_scope is None


def test_pass_row_exists_while_running(store: SqliteSuggestionStore) -> None:
    """The pass creates its pass row up front so the UI can poll it."""
    seen: list[PredictionAttemptStatus] = []

    class WatchingRepository(InMemoryEntityRepository):
        async def get_raw_by_shared_id(self, shared_id: str, language: str | None = None) -> dict[str, Any]:
            row = store.get_pass("run1")
            if row is not None:
                seen.append(row.status)
            return await super().get_raw_by_shared_id(shared_id, language)

    script = """
for d in query_entities_full(mode="by_template", template_name="Case"):
    update_entities([{"shared_id": d["shared_id"], "metadata": {"x": 1}}])
result = "ok"
"""
    asyncio.run(_use_case(store, repository=WatchingRepository(_raws())).run(script, "run1", _budget()))
    assert seen and all(status == PredictionAttemptStatus.RUNNING for status in seen)


# --- subset scoping ----------------------------------------------------------


def test_subset_pass_only_covers_the_selected_entities(store: SqliteSuggestionStore) -> None:
    script = """
for d in query_entities_full(mode="by_template", template_name="Case"):
    update_entities([{"shared_id": d["shared_id"], "metadata": {"excerpt": "scoped"}}])
result = "ok"
"""
    pass_row = asyncio.run(_use_case(store).run(script, "run1", _budget(), entity_scope=["s2"]))
    rows, total = store.query("run1", SuggestionFilter())
    assert total == 1
    assert rows[0].shared_id == "s2"
    assert pass_row.entity_scope == ["s2"]
    assert pass_row.entities_total == 1


# --- whole-entity ops --------------------------------------------------------


def test_whole_entity_ops_become_single_rows_without_verdicts(store: SqliteSuggestionStore) -> None:
    script = """
delete_entities(["s1"])
publish_entities(["s2"])
set_publish_status(["s3"], False)
result = "ok"
"""
    asyncio.run(_use_case(store).run(script, "run1", _budget()))
    rows, _ = store.query("run1", SuggestionFilter())
    assert [(r.op, r.property_name, r.match) for r in rows] == [
        ("delete", None, None),
        ("publish", None, None),
        ("set_publish_status", None, None),
    ]


# --- no-LLM contract ---------------------------------------------------------


def test_a_selection_script_calling_the_llm_fails_loudly(store: SqliteSuggestionStore) -> None:
    """The selection pass binds llm REFUSAL stubs: LLM work is the APPLY script's
    job (the two-script contract), so an LLM-calling selection script dies."""
    from uwazi_admin_agent.use_cases.generate_predictions_use_case import PredictionPassError

    script = """
out = llm_complete("Extract", "doc0")
update_entities([{"shared_id": "s1", "metadata": {"v": out}}])
result = "ok"
"""
    with pytest.raises(PredictionPassError, match="NOT available in a SELECTION script"):
        asyncio.run(_use_case(store).run(script, "run1", _budget()))
    pass_row = store.get_pass("run1")
    assert pass_row is not None
    assert pass_row.status == PredictionAttemptStatus.FAILED


def test_a_script_error_is_failed_and_reraised(store: SqliteSuggestionStore) -> None:
    from uwazi_admin_agent.use_cases.generate_predictions_use_case import PredictionPassError

    script = """
raise ValueError("boom")
"""
    with pytest.raises(PredictionPassError, match="boom"):
        asyncio.run(_use_case(store).run(script, "run1", _budget()))
    pass_row = store.get_pass("run1")
    assert pass_row is not None
    assert pass_row.status == PredictionAttemptStatus.FAILED
    assert "boom" in (pass_row.error or "")


def test_unwired_llm_helpers_fail_the_pass_loudly(store: SqliteSuggestionStore) -> None:
    """Covered by the no-LLM contract now: a selection pass binds refusal
    stubs regardless of wiring, so the distinct 'unwired' case is gone."""
    from uwazi_admin_agent.use_cases.generate_predictions_use_case import PredictionPassError

    script = """
out = llm_complete("x", "y")
result = out
"""
    with pytest.raises(PredictionPassError, match="NOT available in a SELECTION script"):
        asyncio.run(_use_case(store).run(script, "run1", _budget()))


# --- re-runs ----------------------------------------------------------------


def test_rerunning_over_a_subset_overwrites_those_rows(store: SqliteSuggestionStore) -> None:
    script = """
for d in query_entities_full(mode="by_template", template_name="Case"):
    update_entities([{"shared_id": d["shared_id"], "metadata": {"excerpt": "v1"}}])
result = "ok"
"""
    use_case = _use_case(store)
    asyncio.run(use_case.run(script, "run1", _budget()))
    assert store.query("run1", SuggestionFilter())[1] == 3
    script2 = script.replace("v1", "v2")
    asyncio.run(use_case.run(script2, "run1", _budget(), entity_scope=["s1"]))
    rows, total = store.query("run1", SuggestionFilter())
    assert total == 3  # no duplicates
    assert store.query("run1", SuggestionFilter(property_name="excerpt"))[1] == 3
    s1 = [r for r in rows if r.shared_id == "s1"][0]
    # proposed_value is the script's raw payload shape, preserved verbatim.
    assert s1.proposed_value == "v2"


def test_a_second_pass_merges_into_the_same_live_table(store: SqliteSuggestionStore) -> None:
    """There is ONE table per run: a second pass merges into it, never beside it."""
    script = """
for d in query_entities_full(mode="by_template", template_name="Case"):
    update_entities([{"shared_id": d["shared_id"], "metadata": {"x": 1}}])
result = "ok"
"""
    use_case = _use_case(store)
    asyncio.run(use_case.run(script, "run1", _budget()))
    asyncio.run(use_case.run(script, "run1", _budget()))
    assert store.query("run1", SuggestionFilter())[1] == 3
    assert store.get_pass("run1") is not None


# --- statuses survive a re-run ----------------------------------------------


def test_rerun_preserves_a_review_decision(store: SqliteSuggestionStore) -> None:
    script = """
for d in query_entities_full(mode="by_template", template_name="Case"):
    update_entities([{"shared_id": d["shared_id"], "metadata": {"excerpt": "v"}}])
result = "ok"
"""
    use_case = _use_case(store)
    asyncio.run(use_case.run(script, "run1", _budget()))
    rows, _ = store.query("run1", SuggestionFilter())
    store.set_status([rows[0].id or 0], SuggestionStatus.DISCARDED, reason="bad")
    asyncio.run(use_case.run(script, "run1", _budget()))
    after = store.query("run1", SuggestionFilter())[0]
    assert after[0].status == SuggestionStatus.DISCARDED
    assert after[0].discard_reason == "bad"


def test_ledger_reports_exceeded_only_after_a_breach() -> None:
    ledger = LlmLedger(_budget(requests=1))
    assert ledger.exceeded is False
    ledger.reserve()
    ledger.record(LlmUsage(requests=1))
    assert ledger.exceeded is False
    with pytest.raises(Exception):
        ledger.reserve()
    assert ledger.exceeded is True
