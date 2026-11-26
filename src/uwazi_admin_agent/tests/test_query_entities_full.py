"""Isolated unit tests for the bound ``query_entities_full`` bulk-read helper.

The helper exists so a generated script reads ALL entities behind a search in
ONE call: the search modes already fetch every match into
``AgentEntitySearchResult._all_entities``, while the historical script pattern
(search -> ``summary.shared_ids`` -> ``by_ids``) re-fetched each id with ONE
HTTP request per entity (~30 minutes at the 10 000-entity scale). These tests
pin the real-mode pure seam (:func:`build_full_entities_view`) and the shared unknown-mode
error (:func:`full_mode_usage_error`), mirroring ``query_entities``' own test
conventions (``test_search_result_view``).

Isolated: no mocks, no network, literal inputs only.
"""

from __future__ import annotations

from uwazi_admin_agent.use_cases.script_exec_namespace import (
    build_full_entities_view,
    full_mode_usage_error,
)
from uwazi_agent.domain.agent_entity import AgentEntity
from uwazi_agent.domain.agent_entity_search_result import AgentEntitySearchResult
from uwazi_agent.domain.agent_entity_summary import AgentEntitySummary


def _entity(shared_id: str, title: str, template_name: str = "Judgment") -> AgentEntity:
    return AgentEntity(shared_id=shared_id, title=title, template_name=template_name)


def _result(all_entities: list[AgentEntity]) -> AgentEntitySearchResult:
    """Build a result whose ``_all_entities`` carries the full list (mirroring
    ``UwaziApiAdapter._summarize``)."""
    result = AgentEntitySearchResult(
        summary=AgentEntitySummary(count=len(all_entities)),
        examples=all_entities[:3],
    )
    result._all_entities = all_entities
    return result


# --- build_full_entities_view (real mode) -------------------------------------


def test_view_returns_every_entity_as_dict_in_search_order() -> None:
    entities = [_entity(f"S{i}", f"Title {i}") for i in range(5)]

    view = build_full_entities_view(_result(entities))

    assert view == [e.model_dump() for e in entities]
    assert [d["shared_id"] for d in view] == ["S0", "S1", "S2", "S3", "S4"]


def test_view_dicts_match_by_ids_dump_shape() -> None:
    view = build_full_entities_view(_result([_entity("S0", "T0", "Report")]))

    assert set(view[0].keys()) == {
        "shared_id",
        "title",
        "template_name",
        "metadata",
        "language",
        "published",
        "creation_date",
        "edit_date",
    }


def test_view_empty_result_returns_empty_list() -> None:
    assert build_full_entities_view(_result([])) == []


def test_view_keeps_metadata_values() -> None:
    e = _entity("S0", "T0")
    e.metadata = {"document_status": ["draft"]}

    view = build_full_entities_view(_result([e]))

    assert view[0]["metadata"] == {"document_status": ["draft"]}


def test_view_carries_all_entities_not_summary_sample() -> None:
    # The summary's shared_ids stay a 3-sample (LLM-context truncation); the
    # view must NOT inherit it - a script grouping by template needs every row.
    entities = [_entity(f"S{i}", f"T{i}") for i in range(7)]
    result = _result(entities)
    result.summary = AgentEntitySummary(count=7, shared_ids=["S0", "S1", "S2"])

    view = build_full_entities_view(result)

    assert len(view) == 7
    assert [d["shared_id"] for d in view] == [f"S{i}" for i in range(7)]


# --- full_mode_usage_error -----------------------------------------------------


def test_full_mode_usage_error_lists_only_the_search_modes() -> None:
    msg = full_mode_usage_error("by_ids")

    assert msg.startswith("Error: unknown mode 'by_ids'.")
    assert "Use one of: 'by_text', 'by_filter', 'by_template'." in msg


def test_full_mode_usage_error_names_query_entities_for_known_ids() -> None:
    msg = full_mode_usage_error("by_ids")

    assert "query_entities(mode='by_ids'" in msg
