"""Unit tests for the entity-agent optimization changes:

* :class:`EntityStore` trim cache (add, get_many, misses, invalidate_ids, clear).
* :func:`query_entities` ``by_ids`` mode serving hits from the cache and
  falling back to the API for misses (in the documented input order).
* :func:`set_entities_publish_status` ``auto_skip_already_in_target_state``
  pre-flight and per-id stitching.
"""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock


from uwazi_agent.domain.agent_entity import AgentEntity
from uwazi_agent.domain.agent_entity_mutation_result import AgentEntityMutationResult
from uwazi_agent.domain.agent_entity_search_result import AgentEntitySearchResult
from uwazi_agent.domain.agent_entity_summary import AgentEntitySummary
from uwazi_agent.domain.agent_publish_status import AgentPublishStatus
from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies
from uwazi_agent.use_cases.tools.entity_store import EntityStore
from uwazi_agent.use_cases.tools.query_entities import query_entities
from uwazi_agent.use_cases.tools.set_entities_publish_status import set_entities_publish_status


def _run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


def _ctx(deps: UwaziAgentToolsDependencies) -> Any:
    class _C:
        pass
    c = _C()
    c.deps = deps
    return c


def _deps_with(api: Any, store: EntityStore | None = None) -> UwaziAgentToolsDependencies:
    return UwaziAgentToolsDependencies.model_construct(
        entity_api=api,
        entity_store=store or EntityStore(),
        tool_cache=MagicMock(),
        tool_progress=[],
        thesauri_api=MagicMock(),
        template_api=MagicMock(),
        template_mapper=MagicMock(),
        stats_api=MagicMock(),
        relationship_type_api=MagicMock(),
        relationship_api=MagicMock(),
        page_api=MagicMock(),
        settings_api=MagicMock(),
        schema_store=MagicMock(),
    )


class TestEntityStoreTrimCache:
    def test_add_populates_cache_and_entities(self):
        store = EntityStore()
        e = AgentEntity(shared_id="a", title="t", template_name="Films", language="en")
        store.add_entities([e])
        assert store.cache_get_many(["a"], "en") == [e]
        assert [entity.shared_id for entity in store.entities] == ["a"]

    def test_cache_get_many_only_returns_hits_in_input_order(self):
        store = EntityStore()
        store.add_entities([
            AgentEntity(shared_id="a", title="t1", template_name="Films", language="en"),
            AgentEntity(shared_id="b", title="t2", template_name="Films", language="en"),
        ])
        result = store.cache_get_many(["b", "nope", "a"], "en")
        # input order preserved, miss dropped.
        assert [e.shared_id for e in result] == ["b", "a"]

    def test_cache_misses_returns_unseen_ids(self):
        store = EntityStore()
        store.add_entities([AgentEntity(shared_id="a", title="t", template_name="Films", language="en")])
        assert store.cache_misses(["a", "b"], "en") == ["b"]

    def test_cache_is_per_language(self):
        store = EntityStore()
        store.add_entities([
            AgentEntity(shared_id="a", title="en", template_name="Films", language="en"),
            AgentEntity(shared_id="a", title="fr", template_name="Films", language="fr"),
        ])
        assert store.cache_get_many(["a"], "en")[0].title == "en"
        assert store.cache_get_many(["a"], "fr")[0].title == "fr"
        assert store.cache_get_many(["a"], "es") == []

    def test_invalidate_ids_drops_cache_and_entities(self):
        store = EntityStore()
        store.add_entities([
            AgentEntity(shared_id="a", title="t", template_name="Films", language="en"),
            AgentEntity(shared_id="b", title="t", template_name="Films", language="en"),
        ])
        store.invalidate_ids(["a"])
        assert store.cache_get_many(["a"], "en") == []
        # 'a' is a miss; 'b' is still cached from the original add.
        assert store.cache_misses(["a", "b"], "en") == ["a"]
        assert [entity.shared_id for entity in store.entities] == ["b"]

    def test_clear_resets_both(self):
        store = EntityStore()
        store.add_entities([AgentEntity(shared_id="a", title="t", template_name="Films", language="en")])
        store.clear()
        assert store._trim_cache == {}
        assert store.entities == []


class TestQueryEntitiesByIdsCache:
    def test_full_cache_hit_does_not_call_api(self):
        api = AsyncMock()
        e1 = AgentEntity(shared_id="s1", title="t1", template_name="Films", language="en")
        e2 = AgentEntity(shared_id="s2", title="t2", template_name="Films", language="en")
        store = EntityStore()
        store.add_entities([e1, e2])
        api.get_entities_by_shared_ids = AsyncMock(return_value=[])

        result = _run(query_entities(_ctx(_deps_with(api, store)), mode="by_ids", shared_ids=["s1", "s2"]))
        assert isinstance(result, list)
        assert [e.shared_id for e in result] == ["s1", "s2"]
        api.get_entities_by_shared_ids.assert_not_called()

    def test_cache_miss_triggers_api_for_missing_only(self):
        api = AsyncMock()
        e1 = AgentEntity(shared_id="s1", title="t1", template_name="Films", language="en")
        e3 = AgentEntity(shared_id="s3", title="t3", template_name="Films", language="en")
        store = EntityStore()
        store.add_entities([e1])
        api.get_entities_by_shared_ids = AsyncMock(return_value=[e3])

        result = _run(query_entities(_ctx(_deps_with(api, store)), mode="by_ids", shared_ids=["s1", "s2", "s3"]))
        # s1 from cache, s2 unknown (api didn't return it), s3 from api.
        assert [e.shared_id for e in result] == ["s1", "s3"]
        api.get_entities_by_shared_ids.assert_called_once()
        call = api.get_entities_by_shared_ids.call_args
        assert call.kwargs["shared_ids"] == ["s2", "s3"]


class TestQueryEntitiesErrorMessages:
    def test_by_text_without_search_term_returns_error(self):
        deps = _deps_with(AsyncMock(), EntityStore())
        result = _run(query_entities(_ctx(deps), mode="by_text"))
        assert isinstance(result, str) and result.startswith("Error")
        assert "search_term" in result

    def test_by_template_without_template_returns_error(self):
        deps = _deps_with(AsyncMock(), EntityStore())
        result = _run(query_entities(_ctx(deps), mode="by_template"))
        assert isinstance(result, str) and "template_name" in result

    def test_by_ids_without_ids_returns_error(self):
        deps = _deps_with(AsyncMock(), EntityStore())
        result = _run(query_entities(_ctx(deps), mode="by_ids"))
        assert isinstance(result, str) and "shared_ids" in result

    def test_unknown_mode_returns_error(self):
        deps = _deps_with(AsyncMock(), EntityStore())
        result = _run(query_entities(_ctx(deps), mode="by_quantum"))
        assert isinstance(result, str) and "by_quantum" in result

    def test_no_entity_api_returns_error(self):
        deps = _deps_with(None, EntityStore())
        result = _run(query_entities(_ctx(deps), mode="by_text", search_term="x"))
        assert isinstance(result, str) and "entity_api" in result


class TestQueryEntitiesOverflowHint:
    def test_by_text_marks_python_handoff_when_count_exceeds_limit(self, monkeypatch):
        # Force the cap to 1 so a single-hit search trips the handoff.
        monkeypatch.setattr("uwazi_agent.use_cases.tools.query_entities.ENTITIES_LIMIT_FOR_LLM_MODEL", 1)
        api = AsyncMock()
        sample = AgentEntity(shared_id="s1", title="Film A", template_name="Films", language="en")
        result_obj = AgentEntitySearchResult.model_construct()
        result_obj._all_entities = [sample]
        summary = AgentEntitySummary.model_construct(count=2, by_template={"Films": 2}, sample_titles=["Film A"], shared_ids=["s1"])
        result_obj.summary = summary
        result_obj.examples = [sample]
        api.search_entities_by_text = AsyncMock(return_value=result_obj)

        store = EntityStore()
        deps = _deps_with(api, store)
        r = _run(query_entities(_ctx(deps), mode="by_text", search_term="Film"))
        assert r.summary.note is not None and "Python agent" in r.summary.note
        assert store.needs_python_agent is True

    def test_by_ids_marks_python_handoff_when_count_exceeds_limit(self, monkeypatch):
        monkeypatch.setattr("uwazi_agent.use_cases.tools.query_entities.ENTITIES_LIMIT_FOR_LLM_MODEL", 1)
        api = AsyncMock()
        e1 = AgentEntity(shared_id="s1", title="t1", template_name="Films", language="en")
        e2 = AgentEntity(shared_id="s2", title="t2", template_name="Films", language="en")
        api.get_entities_by_shared_ids = AsyncMock(return_value=[e1, e2])
        deps = _deps_with(api, EntityStore())
        r = _run(query_entities(_ctx(deps), mode="by_ids", shared_ids=["s1", "s2"]))
        assert isinstance(r, str) and "Python agent" in r
        assert deps.entity_store.needs_python_agent is True


class TestSetPublishStatusAutoSkip:
    def test_auto_skip_drops_already_published_ids(self):
        api = AsyncMock()
        api.get_publish_status = AsyncMock(return_value=[
            AgentPublishStatus(shared_id="s1", published=True, permissions=[]),
            AgentPublishStatus(shared_id="s2", published=False, permissions=[]),
        ])
        api.set_entities_publish_status = AsyncMock(return_value=[
            AgentEntityMutationResult(shared_id="s2", success=True),
        ])
        deps = _deps_with(api, EntityStore())
        out = _run(set_entities_publish_status(_ctx(deps), ["s1", "s2"], published=True))
        assert isinstance(out, list)
        assert [r.shared_id for r in out] == ["s1", "s2"]
        # s1 was already published -> skipped, s2 went through.
        assert out[0].success is True and out[0].note == "already_in_target_state"
        assert out[1].success is True and out[1].note is None
        # The underlying adapter only received s2.
        api.set_entities_publish_status.assert_called_once()
        assert api.set_entities_publish_status.call_args.kwargs["shared_ids"] == ["s2"]

    def test_auto_skip_disabled_sends_all_ids(self):
        api = AsyncMock()
        api.get_publish_status = AsyncMock(return_value=[
            AgentPublishStatus(shared_id="s1", published=True, permissions=[]),
        ])
        api.set_entities_publish_status = AsyncMock(return_value=[
            AgentEntityMutationResult(shared_id="s1", success=True),
        ])
        deps = _deps_with(api, EntityStore())
        _run(set_entities_publish_status(_ctx(deps), ["s1"], published=True, auto_skip_already_in_target_state=False))
        api.set_entities_publish_status.assert_called_once()
        assert api.set_entities_publish_status.call_args.kwargs["shared_ids"] == ["s1"]

    def test_auto_skip_preflight_failure_falls_back_to_full_call(self):
        from uwazi_api.domain.exceptions import DomainError

        api = AsyncMock()
        api.get_publish_status = AsyncMock(side_effect=DomainError("boom"))
        api.set_entities_publish_status = AsyncMock(return_value=[
            AgentEntityMutationResult(shared_id="s1", success=True),
        ])
        deps = _deps_with(api, EntityStore())
        out = _run(set_entities_publish_status(_ctx(deps), ["s1"], published=True))
        # The pre-flight exception is swallowed, all ids are still sent.
        api.set_entities_publish_status.assert_called_once()
        assert api.set_entities_publish_status.call_args.kwargs["shared_ids"] == ["s1"]
        assert out[0].success is True
