"""Pure tests for :func:`build_search_result_view` (Part A — full shared_ids).

The bound ``query_entities`` search modes must surface the FULL list of
shared_ids (from ``AgentEntitySearchResult._all_entities``) instead of the
LLM-context-truncated ``summary.shared_ids`` (capped to 3 by
``UwaziApiAdapter._summarize``). A script that discovers entities to group/merge
needs every id. These tests construct a literal ``AgentEntitySearchResult`` with
``_all_entities`` populated and assert the view carries the full id list while
preserving the rest of the summary and the sample examples.

Isolated: no mocks, no network, literal inputs only.
"""

from __future__ import annotations

from types import SimpleNamespace

from uwazi_admin_agent.use_cases.script_exec_namespace import build_search_result_view
from uwazi_agent.domain.agent_entity import AgentEntity
from uwazi_agent.domain.agent_entity_search_result import AgentEntitySearchResult
from uwazi_agent.domain.agent_entity_summary import AgentEntitySummary


def _entity(shared_id: str, title: str, template_name: str = "Judgment") -> AgentEntity:
    return AgentEntity(shared_id=shared_id, title=title, template_name=template_name)


def _result(all_entities: list[AgentEntity], sample_ids: list[str]) -> AgentEntitySearchResult:
    """Build a result whose truncated ``summary.shared_ids`` is ``sample_ids``
    but whose ``_all_entities`` carries the full list (mirroring _summarize)."""
    by_template: dict[str, int] = {}
    for e in all_entities:
        by_template[e.template_name] = by_template.get(e.template_name, 0) + 1
    summary = AgentEntitySummary(
        count=len(all_entities),
        by_template=by_template,
        sample_titles=[e.title for e in all_entities[: len(sample_ids)]],
        shared_ids=list(sample_ids),
    )
    examples = all_entities[: len(sample_ids)]
    result = AgentEntitySearchResult(summary=summary, examples=examples)
    result._all_entities = all_entities
    return result


def test_full_shared_ids_override_truncated_sample() -> None:
    entities = [_entity(f"S{i}", f"Title {i}") for i in range(7)]
    result = _result(entities, sample_ids=["S0", "S1", "S2"])  # truncated to 3

    view = build_search_result_view(result)

    assert isinstance(view, SimpleNamespace)
    # The view exposes ALL 7 ids, not the 3-sample on the original summary.
    assert view.summary.shared_ids == [f"S{i}" for i in range(7)]
    assert view.summary.count == 7


def test_empty_all_entities_yields_empty_ids() -> None:
    result = _result([], sample_ids=[])

    view = build_search_result_view(result)

    assert view.summary.shared_ids == []
    assert view.summary.count == 0


def test_preserves_count_by_template_sample_titles_and_note() -> None:
    entities = [_entity("A1", "A1", "Judgment"), _entity("A2", "A2", "Judgment"), _entity("B1", "B1", "Report")]
    summary = AgentEntitySummary(
        count=3,
        by_template={"Judgment": 2, "Report": 1},
        sample_titles=["A1", "A2", "B1"],
        shared_ids=["A1", "A2", "B1"],
        note="3 entities found and stored in the entity store.",
    )
    result = AgentEntitySearchResult(summary=summary, examples=entities)
    result._all_entities = entities

    view = build_search_result_view(result)

    assert view.summary.count == 3
    assert view.summary.by_template == {"Judgment": 2, "Report": 1}
    assert view.summary.sample_titles == ["A1", "A2", "B1"]
    assert view.summary.note == "3 entities found and stored in the entity store."


def test_examples_are_the_sample_dicts() -> None:
    entities = [_entity("S0", "T0"), _entity("S1", "T1"), _entity("S2", "T2")]
    result = _result(entities, sample_ids=["S0", "S1", "S2"])

    view = build_search_result_view(result)

    # examples are the (sample) model_dump dicts, in order.
    assert view.examples == [e.model_dump() for e in entities]


def test_does_not_mutate_original_summary() -> None:
    entities = [_entity(f"S{i}", f"T{i}") for i in range(5)]
    result = _result(entities, sample_ids=["S0", "S1", "S2"])

    _ = build_search_result_view(result)

    # The original (truncated) summary is untouched.
    assert result.summary.shared_ids == ["S0", "S1", "S2"]


def test_ids_without_shared_id_are_skipped() -> None:
    e0 = _entity("S0", "T0")
    e1 = AgentEntity(shared_id="", title="T1", template_name="Judgment")
    result = _result([e0, e1], sample_ids=["S0"])

    view = build_search_result_view(result)

    # The empty shared_id is dropped; only the real id surfaces.
    assert view.summary.shared_ids == ["S0"]
