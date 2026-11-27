"""Isolated tests for the validation pass's read scoping (per AGENTS.md).

The scoring pass is the planning pass over the SAME script, with the frozen
sample prepended to a target-template read. Two reported failures are pinned
here:

* a task scoped to "only entities where the target property is empty" filtered
  the measured property server-side, so every held-out entity (which carries
  the measured value by construction) was filtered out and the pass recorded
  nothing — the "the test set is broken / the rows never appear" report;
* a target-template read narrowed to the sample emptied a relationship catalog
  that lives in the same template, so every row recorded ``skipped``; and with
  no ordering at all a script could slice the sample away.

Real objects: a tiny in-memory search surface and the real bound namespace.
"""

from __future__ import annotations

import asyncio
from typing import Any

from uwazi_admin_agent.use_cases.script_exec_namespace import build_dry_run_namespace
from uwazi_admin_agent.use_cases.script_exec_namespace.scope import (
    evaluation_filters,
    validation_evaluation_reads,
)
from uwazi_agent.domain.agent_entity import AgentEntity
from uwazi_agent.domain.agent_entity_search_result import AgentEntitySearchResult
from uwazi_agent.domain.agent_entity_summary import AgentEntitySummary
from uwazi_agent.domain.agent_search_filter import AgentSearchFilter


def _result(entities: list[AgentEntity]) -> AgentEntitySearchResult:
    result = AgentEntitySearchResult(
        summary=AgentEntitySummary(
            count=len(entities),
            by_template={"Recommendation": len(entities)} if entities else {},
            sample_titles=[entity.title or "" for entity in entities[:3]],
            shared_ids=[entity.shared_id for entity in entities],
        ),
        examples=entities[:3],
    )
    result._all_entities = entities
    return result


class _SearchApi:
    """A real (tiny) search surface: records the filters and limits it received."""

    def __init__(self, entities: list[AgentEntity]) -> None:
        self.entities = entities
        self.seen_filters: list[list[Any]] = []
        self.seen_limits: list[int] = []

    async def search_entities_by_filter(self, template_name, filters, language, limit, published=None):
        selected = [entity for entity in self.entities if entity.template_name == template_name]
        for condition in filters or []:
            if condition.values is not None:
                selected = [
                    entity
                    for entity in selected
                    if set(condition.values) & set(entity.metadata.get(condition.property_name) or [])
                ]
        self.seen_filters.append(list(filters or []))
        self.seen_limits.append(limit)
        return _result(selected)

    async def get_entities_by_template(self, template_name, language, limit):
        selected = [entity for entity in self.entities if entity.template_name == template_name]
        self.seen_filters.append([])
        self.seen_limits.append(limit)
        return _result(selected)

    async def get_entities_by_shared_ids(self, shared_ids, language, limit=10000):
        by_id = {entity.shared_id: entity for entity in self.entities}
        return [by_id[shared_id] for shared_id in shared_ids if shared_id in by_id][:limit]


def _in_namespace(api: _SearchApi, held: set[str], templates: set[str], property_name: str, call: Any) -> Any:
    """Run ``call`` against a real dry-run namespace with the evaluation scope applied."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        namespace = build_dry_run_namespace(
            entity_api=api,
            loop=loop,
            file_repository=None,
            default_language="en",
            dry_run_records=[],
        )
        validation_evaluation_reads(namespace, held, templates, property_name)
        return call(namespace)
    finally:
        loop.close()


def test_the_empty_scope_filter_is_dropped_for_the_scoring_pass() -> None:
    """Every held-out entity CARRIES the measured value: filtering on "unset" drops them all."""
    filters = [AgentSearchFilter(property_name="states", values=["missing"])]
    assert evaluation_filters(filters, "states") is None
    # A filter on another property is the task's own selection and stays.
    other = [AgentSearchFilter(property_name="judge", values=["Doe"])]
    assert evaluation_filters(other, "states") == other


def test_a_target_template_read_returns_the_sample_first_and_keeps_the_rest() -> None:
    api = _SearchApi(
        [
            AgentEntity(shared_id="other", title="Other", template_name="Recommendation"),
            AgentEntity(shared_id="kept", title="Kept", template_name="Recommendation"),
        ]
    )

    found = _in_namespace(
        api,
        {"kept"},
        {"Recommendation"},
        "states",
        lambda namespace: namespace["query_entities"](
            "by_filter",
            template_name="Recommendation",
            filters=[{"property_name": "states", "values": ["missing"]}],
            limit=2,
        ),
    )

    # The sample is FIRST (a script that slices starts at the scored entity) and
    # the rest of the page stays readable: a narrowed read emptied a catalog that
    # lives in the same template.
    assert found.summary.shared_ids == ["kept", "other"]
    # The measured-property filter was dropped: it selects exactly the entities the
    # score cannot use, so keeping it made the pass record nothing at all.
    assert api.seen_filters == [[]]


def test_a_reference_catalog_read_is_not_narrowed_to_the_sample() -> None:
    """A task that looks values up in another template must still see that catalog."""
    api = _SearchApi(
        [
            AgentEntity(shared_id="kept", title="Kept", template_name="Recommendation"),
            AgentEntity(shared_id="state-a", title="State A", template_name="State"),
        ]
    )

    found = _in_namespace(
        api,
        {"kept"},
        {"Recommendation"},
        "states",
        lambda namespace: namespace["query_entities"](
            "by_template",
            template_name="State",
            limit=50,
        ),
    )

    assert found.summary.shared_ids == ["state-a"]
    assert api.seen_limits == [50]


def test_an_explicit_id_read_is_not_narrowed() -> None:
    """An explicit-id lookup is not discovery: a relationship script must fetch any id."""
    api = _SearchApi(
        [
            AgentEntity(shared_id="other", title="Other", template_name="Recommendation"),
            AgentEntity(shared_id="kept", title="Kept", template_name="Recommendation"),
        ]
    )

    found = _in_namespace(
        api,
        {"kept"},
        {"Recommendation"},
        "states",
        lambda namespace: namespace["query_entities"](mode="by_ids", shared_ids=["kept", "other"]),
    )

    assert [entity["shared_id"] for entity in found] == ["kept", "other"]


def test_a_catalog_in_a_target_template_is_complete() -> None:
    """The reported shape: a relationship resolves values from the SAME template.

    Narrowing this read emptied the catalog, so every held-out row recorded
    ``skipped`` and the validation table showed no suggested value.
    """
    api = _SearchApi(
        [
            AgentEntity(shared_id="other", title="Other", template_name="Recommendation"),
            AgentEntity(shared_id="kept", title="Kept", template_name="Recommendation"),
        ]
    )

    found = _in_namespace(
        api,
        {"kept"},
        {"Recommendation"},
        "states",
        lambda namespace: namespace["query_entities_full"]("by_template", template_name="Recommendation"),
    )

    assert [entity["shared_id"] for entity in found] == ["kept", "other"]
