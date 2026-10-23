"""Isolated unit tests for the script LLM helpers + budget enforcement (per AGENTS.md).

Real objects throughout: a concrete in-memory :class:`LlmRuntimePort` subclass
whose ``complete``/``map_batch`` are pure functions (a real implementation, not
a mock), a real :class:`LlmLedger`, and the real
:func:`build_llm_script_helpers` / :func:`build_dry_run_namespace` /
:func:`run_script_sync` wiring. No network, no instance, no monkeypatching.
"""

import asyncio

import pytest

from uwazi_admin_agent.domain.llm_budget import (
    LlmBudget,
    LlmBudgetExceededError,
    LlmLedger,
    LlmPrice,
    LlmUsage,
)
from uwazi_admin_agent.domain.suggestion import SuggestionStatus
from uwazi_admin_agent.domain.suggestion_scoring import records_to_suggestions
from uwazi_admin_agent.ports.llm_runtime_port import LlmBatchResult, LlmCallResult, LlmRuntimePort
from uwazi_admin_agent.use_cases.llm_script_helpers import build_llm_script_helpers
from uwazi_admin_agent.use_cases.script_exec_namespace import (
    build_dry_run_namespace,
    run_script_sync,
    scope_read_result,
)
from uwazi_agent.domain.agent_entity import AgentEntity
from uwazi_agent.domain.agent_entity_create import AgentEntityCreate
from uwazi_agent.domain.agent_entity_mutation_result import AgentEntityMutationResult
from uwazi_agent.domain.agent_entity_search_result import AgentEntitySearchResult
from uwazi_agent.domain.agent_entity_summary import AgentEntitySummary
from uwazi_agent.domain.agent_publish_status import AgentPublishStatus
from uwazi_agent.domain.agent_search_filter import AgentSearchFilter
from uwazi_agent.ports.entity_api_port import EntityApiPort

PRICE = LlmPrice(model="test-model", input_per_mtok=0.001, output_per_mtok=0.002)


class InMemoryEntityApi(EntityApiPort):
    """A real (non-mock) in-memory instance: deterministic reads, no I/O.

    Implements every abstract method so it can be wired into the namespace
    builders as the genuine port. Writes are recorded in a list so a test can
    assert what a script tried to do.
    """

    def __init__(self, entities: list[AgentEntity]) -> None:
        self._entities = list(entities)
        self.updates: list[AgentEntity] = []
        self.delete_calls: list[str] = []

    async def create_entities(self, entities: list[AgentEntityCreate], language: str) -> list[AgentEntityMutationResult]:
        results = []
        for index, entity in enumerate(entities):
            shared_id = f"new-{index}"
            self._entities.append(
                AgentEntity(shared_id=shared_id, template_name=entity.template_name, metadata=dict(entity.metadata))
            )
            results.append(AgentEntityMutationResult(success=True, shared_id=shared_id))
        return results

    async def get_entities_by_shared_ids(
        self, shared_ids: list[str], language: str, limit: int = 10000
    ) -> list[AgentEntity]:
        wanted = set(shared_ids[:limit])
        return [entity for entity in self._entities if entity.shared_id in wanted]

    async def search_entities_by_text(
        self, search_term: str, template_name: str | None, language: str, limit: int
    ) -> AgentEntitySearchResult:
        matched = [e for e in self._entities if search_term in (e.title or "")]
        return self._search_result(matched, limit)

    async def get_entities_by_template(self, template_name: str, language: str, limit: int) -> AgentEntitySearchResult:
        matched = [e for e in self._entities if e.template_name == template_name]
        return self._search_result(matched, limit)

    async def search_entities_by_filter(
        self,
        template_name: str,
        filters: list[AgentSearchFilter],
        language: str,
        limit: int,
        published: bool | None = None,
    ) -> AgentEntitySearchResult:
        matched = [e for e in self._entities if e.template_name == template_name]
        return self._search_result(matched, limit)

    async def update_entities(self, updates: list[AgentEntity], language: str) -> list[AgentEntityMutationResult]:
        self.updates.extend(updates)
        return [AgentEntityMutationResult(success=True, shared_id=u.shared_id) for u in updates]

    async def delete_entities_by_shared_ids(self, shared_ids: list[str]) -> list[AgentEntityMutationResult]:
        self.delete_calls.extend(shared_ids)
        return [AgentEntityMutationResult(success=True, shared_id=sid) for sid in shared_ids]

    async def set_entities_publish_status(self, shared_ids: list[str], published: bool) -> list[AgentEntityMutationResult]:
        return [AgentEntityMutationResult(success=True, shared_id=sid) for sid in shared_ids]

    async def get_publish_status(self, shared_ids: list[str], language: str) -> list[AgentPublishStatus]:
        return [AgentPublishStatus(shared_id=sid, published=True) for sid in shared_ids]

    def _search_result(self, matched: list[AgentEntity], limit: int) -> AgentEntitySearchResult:
        capped = matched[:limit]
        summary = AgentEntitySummary(
            count=len(capped),
            shared_ids=[e.shared_id for e in capped],
            sample_titles=[e.title or "" for e in capped[:3]],
        )
        result = AgentEntitySearchResult(summary=summary, examples=capped[:3])
        result._all_entities = capped
        return result


class EchoRuntime(LlmRuntimePort):
    """A real (non-mock) runtime: deterministic transforms, counted calls."""

    def __init__(self, out_tokens_per_item: int = 10) -> None:
        self.calls = 0
        self.items_seen = 0
        self._out_tokens_per_item = out_tokens_per_item

    async def complete(self, instruction: str, text: str) -> LlmCallResult:
        self.calls += 1
        out = f"{instruction}|{text}"
        return LlmCallResult(output=out, usage=LlmUsage(requests=1, input_tokens=5, output_tokens=self._out_tokens_per_item))

    async def map_batch(self, instruction: str, items: list[str]) -> LlmBatchResult:
        self.calls += 1
        self.items_seen += len(items)
        outputs = [f"{instruction}#{item}" for item in items]
        return LlmBatchResult(
            outputs=outputs,
            usage=LlmUsage(requests=1, input_tokens=len(items) * 5, output_tokens=len(items) * self._out_tokens_per_item),
        )


def _budget(requests: int = 100, cost: float = 10_000.0) -> LlmBudget:
    return LlmBudget(max_cost=cost, max_requests=requests, price=PRICE)


def _helpers(runtime: LlmRuntimePort, ledger: LlmLedger, progress=None, batch_size: int = 10) -> dict:
    loop = asyncio.new_event_loop()
    return build_llm_script_helpers(runtime, ledger, loop, progress=progress, batch_size=batch_size)


# --- unwired helpers fail loudly ---------------------------------------------


def test_helpers_are_loud_stubs_when_unwired() -> None:
    helpers = build_llm_script_helpers(None, None, asyncio.new_event_loop())
    with pytest.raises(RuntimeError, match="wired LlmRuntimePort and approved budget"):
        helpers["llm_complete"]("do it", "text")
    with pytest.raises(RuntimeError, match="wired LlmRuntimePort and approved budget"):
        helpers["llm_map"]("do it", ["a"])


def test_helpers_are_loud_stubs_when_only_budget_is_missing() -> None:
    helpers = build_llm_script_helpers(EchoRuntime(), None, asyncio.new_event_loop())
    with pytest.raises(RuntimeError):
        helpers["llm_complete"]("x", "y")


# --- llm_complete ------------------------------------------------------------


def test_llm_complete_returns_output_and_charges_the_ledger() -> None:
    runtime = EchoRuntime()
    ledger = LlmLedger(_budget())
    helpers = _helpers(runtime, ledger)
    assert helpers["llm_complete"]("Summarize", "a document") == "Summarize|a document"
    assert runtime.calls == 1
    assert ledger.snapshot() == LlmUsage(requests=1, input_tokens=5, output_tokens=10)


def test_llm_complete_aborts_before_the_call_when_the_budget_is_exhausted() -> None:
    runtime = EchoRuntime()
    ledger = LlmLedger(_budget(requests=1))
    helpers = _helpers(runtime, ledger)
    helpers["llm_complete"]("a", "b")
    with pytest.raises(LlmBudgetExceededError) as excinfo:
        helpers["llm_complete"]("c", "d")
    assert excinfo.value.reason == "requests"
    # The refused call was never issued: exactly one request reached the runtime.
    assert runtime.calls == 1


def test_llm_complete_reports_progress() -> None:
    seen: list[int] = []
    helpers = _helpers(EchoRuntime(), LlmLedger(_budget()), progress=seen.append)
    helpers["llm_complete"]("a", "b")
    helpers["llm_complete"]("c", "d")
    assert seen == [1, 1]


# --- llm_map -----------------------------------------------------------------


def test_llm_map_batches_by_batch_size() -> None:
    runtime = EchoRuntime()
    ledger = LlmLedger(_budget())
    helpers = _helpers(runtime, ledger, batch_size=4)
    items = [f"item{i}" for i in range(10)]
    outputs = helpers["llm_map"]("Extract", items)
    assert outputs == [f"Extract#item{i}" for i in range(10)]
    # 10 items at batch 4 -> 3 requests (4 + 4 + 2).
    assert runtime.calls == 3
    assert runtime.items_seen == 10
    assert ledger.snapshot().requests == 3


def test_llm_map_reports_progress_per_batch() -> None:
    seen: list[int] = []
    helpers = _helpers(EchoRuntime(), LlmLedger(_budget()), progress=seen.append, batch_size=4)
    helpers["llm_map"]("x", [f"i{n}" for n in range(10)])
    assert seen == [4, 4, 2]


def test_llm_map_on_empty_items_makes_no_calls() -> None:
    runtime = EchoRuntime()
    ledger = LlmLedger(_budget())
    helpers = _helpers(runtime, ledger)
    assert helpers["llm_map"]("x", []) == []
    assert runtime.calls == 0
    assert ledger.snapshot() == LlmUsage()


def test_llm_map_aborts_mid_pass_at_the_ceiling() -> None:
    runtime = EchoRuntime()
    ledger = LlmLedger(_budget(requests=2))
    helpers = _helpers(runtime, ledger, batch_size=2)
    with pytest.raises(LlmBudgetExceededError):
        helpers["llm_map"]("x", [f"i{n}" for n in range(10)])
    # Two batches ran; the third was refused before the request.
    assert runtime.calls == 2
    assert ledger.snapshot().requests == 2


# --- budget enforcement end-to-end through a real script ---------------------


def test_real_script_uses_the_llm_helper_and_records_its_writes() -> None:
    """A script's llm_map call reaches the runtime and its writes are recorded."""
    runtime = EchoRuntime()
    ledger = LlmLedger(_budget(requests=3))
    records: list[dict] = []
    namespace = build_dry_run_namespace(
        entity_api=None,
        loop=asyncio.new_event_loop(),
        file_repository=None,
        default_language="en",
        dry_run_records=records,
        llm_runtime=runtime,
        llm_ledger=ledger,
        apply_mode=True,
    )
    # 10 items at the default batch size of 10 -> exactly one request.
    script = """
outputs = llm_map("Extract a title", ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"])
for index, out in enumerate(outputs):
    update_entities([{"shared_id": f"s{index}", "metadata": {"extracted": out}}])
result = len(outputs)
"""
    result, error = run_script_sync(script, namespace)
    assert error is None, error
    assert result == "10"
    assert runtime.calls == 1
    assert ledger.snapshot().requests == 1
    assert len(records) == 10
    assert records[0]["metadata"] == {"extracted": "Extract a title#a"}


def test_real_script_aborts_at_the_request_ceiling_with_llm_batch_size_one() -> None:
    runtime = EchoRuntime()
    # 3 requests allowed, one per item -> the 4th item's call is refused.
    ledger = LlmLedger(_budget(requests=3))
    records: list[dict] = []
    namespace = build_dry_run_namespace(
        entity_api=None,
        loop=asyncio.new_event_loop(),
        file_repository=None,
        default_language="en",
        dry_run_records=records,
        llm_runtime=runtime,
        llm_ledger=ledger,
        apply_mode=True,
    )
    # A script that calls the LLM and writes one record per iteration, so the
    # records prove how far the pass got before the budget stopped it.
    script = """
for i in range(6):
    out = llm_complete("Extract", f"doc{i}")
    update_entities([{"shared_id": f"s{i}", "metadata": {"v": out}}])
result = "done"
"""
    result, error = run_script_sync(script, namespace)
    assert result is None
    assert error is not None and "LlmBudgetExceededError" in error
    assert runtime.calls == 3
    # Four writes were recorded before the failing call (the 4th llm_complete).
    assert [r["shared_id"] for r in records] == ["s0", "s1", "s2"]


def test_records_before_abort_convert_to_suggestions() -> None:
    """The abort keeps everything persisted so far (the pass is not lost)."""
    runtime = EchoRuntime()
    ledger = LlmLedger(_budget(requests=2))
    records: list[dict] = []
    namespace = build_dry_run_namespace(
        entity_api=None,
        loop=asyncio.new_event_loop(),
        file_repository=None,
        default_language="en",
        dry_run_records=records,
        llm_runtime=runtime,
        llm_ledger=ledger,
        apply_mode=True,
    )
    script = """
for i in range(5):
    out = llm_complete("Extract", f"doc{i}")
    update_entities([{"shared_id": f"s{i}", "metadata": {"v": out}}])
"""
    _result, error = run_script_sync(script, namespace)
    assert error is not None and "LlmBudgetExceededError" in error
    suggestions = records_to_suggestions("run1", records)
    assert [(s.shared_id, s.property_name) for s in suggestions] == [("s0", "v"), ("s1", "v")]
    assert all(s.status == SuggestionStatus.PENDING for s in suggestions)


# --- subset scoping ----------------------------------------------------------


def test_scope_read_result_filters_lists_dicts_and_search_results() -> None:
    from types import SimpleNamespace as NS

    from uwazi_agent.domain.agent_entity_summary import AgentEntitySummary

    assert scope_read_result("Error: nope", {"a"}) == "Error: nope"
    assert scope_read_result([{"shared_id": "a"}, {"shared_id": "b"}], {"a"}) == [{"shared_id": "a"}]
    summary = AgentEntitySummary(count=2, shared_ids=["a", "b"], by_template={"T": 2})
    view = NS(summary=summary, examples=[{"shared_id": "a"}, {"shared_id": "b"}])
    filtered = scope_read_result(view, {"b"})
    assert filtered.summary.shared_ids == ["b"]
    assert filtered.summary.count == 1
    assert filtered.examples == [{"shared_id": "b"}]


def test_subset_scope_restricts_a_script_that_searches_a_whole_template() -> None:
    """The identical script sees only the selected entities (subset prediction)."""
    api = InMemoryEntityApi(
        [
            AgentEntity(shared_id="s1", title="One", template_name="Case"),
            AgentEntity(shared_id="s2", title="Two", template_name="Case"),
            AgentEntity(shared_id="s3", title="Three", template_name="Case"),
            AgentEntity(shared_id="s4", title="Four", template_name="Case"),
            AgentEntity(shared_id="s5", title="Five", template_name="Case"),
        ]
    )
    records: list[dict] = []
    namespace = build_dry_run_namespace(
        entity_api=api,
        loop=asyncio.new_event_loop(),
        file_repository=None,
        default_language="en",
        dry_run_records=records,
        entity_scope={"s1", "s2"},
    )
    script = """
res = query_entities(mode="by_template", template_name="Case")
for sid in res.summary.shared_ids:
    update_entities([{"shared_id": sid, "metadata": {"seen": True}}])
result = sorted(res.summary.shared_ids)
"""
    result, error = run_script_sync(script, namespace)
    assert error is None, error
    # The script asked for the whole template (5 entities) and saw 2.
    assert result == "['s1', 's2']"
    assert [r["shared_id"] for r in records] == ["s1", "s2"]


def test_subset_scope_restricts_query_entities_full_too() -> None:
    api = InMemoryEntityApi([AgentEntity(shared_id=f"s{n}", title=f"T{n}", template_name="Case") for n in range(1, 6)])
    records: list[dict] = []
    namespace = build_dry_run_namespace(
        entity_api=api,
        loop=asyncio.new_event_loop(),
        file_repository=None,
        default_language="en",
        dry_run_records=records,
        entity_scope={"s3"},
    )
    script = """
entities = query_entities_full(mode="by_template", template_name="Case")
result = [e["shared_id"] for e in entities]
"""
    result, error = run_script_sync(script, namespace)
    assert error is None, error
    assert result == "['s3']"


def test_no_scope_leaves_discovery_unrestricted() -> None:
    """Without a scope the identical script sees every entity (whole-instance pass)."""
    api = InMemoryEntityApi([AgentEntity(shared_id=f"s{n}", title=f"T{n}", template_name="Case") for n in range(1, 6)])
    records: list[dict] = []
    namespace = build_dry_run_namespace(
        entity_api=api,
        loop=asyncio.new_event_loop(),
        file_repository=None,
        default_language="en",
        dry_run_records=records,
    )
    script = """
entities = query_entities_full(mode="by_template", template_name="Case")
result = [e["shared_id"] for e in entities]
"""
    result, error = run_script_sync(script, namespace)
    assert error is None, error
    assert result == "['s1', 's2', 's3', 's4', 's5']"


def test_out_of_scope_entity_reads_return_empty_shapes() -> None:
    api = InMemoryEntityApi([AgentEntity(shared_id="s1", title="One", template_name="Case")])
    records: list[dict] = []
    namespace = build_dry_run_namespace(
        entity_api=api,
        loop=asyncio.new_event_loop(),
        file_repository=None,
        default_language="en",
        dry_run_records=records,
        entity_scope={"s1"},
    )
    # An out-of-scope id short-circuits to the "nothing here" shape the contract
    # documents; the in-scope id reaches the real (unwired) repository helper.
    assert namespace["get_entity_files"]("other") == []
    assert namespace["get_segmentation"]("other") is None
    with pytest.raises(RuntimeError, match="entity_repository"):
        namespace["get_entity_files"]("s1")
