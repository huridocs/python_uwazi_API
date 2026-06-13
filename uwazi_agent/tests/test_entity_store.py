"""Unit tests for EntityStore.

Covers the in-memory entity cache and the data-payload helpers used to
share prepared data between agents (e.g. timeline entries produced by the
Python agent and consumed by the page agent).
"""

import pytest

from uwazi_agent.domain.agent_entity import AgentEntity
from uwazi_agent.use_cases.tools.entity_store import EntityStore


def _make_entity(shared_id: str, template: str = "Books") -> AgentEntity:
    return AgentEntity(
        shared_id=shared_id,
        title=f"Title-{shared_id}",
        template_name=template,
        metadata={},
        language="en",
        published=True,
    )


class TestEntityStoreDataPayloads:
    def test_set_and_get_data_payload(self):
        store = EntityStore()
        store.set_data_payload("timeline", [{"date": "2024-01-01", "title": "A"}])
        assert store.get_data_payload("timeline") == [{"date": "2024-01-01", "title": "A"}]

    def test_get_data_payload_returns_default_when_missing(self):
        store = EntityStore()
        assert store.get_data_payload("missing") is None
        assert store.get_data_payload("missing", default="default") == "default"

    def test_has_data_payload(self):
        store = EntityStore()
        assert not store.has_data_payload("timeline")
        store.set_data_payload("timeline", [])
        assert store.has_data_payload("timeline")

    def test_list_data_payload_keys_sorted(self):
        store = EntityStore()
        store.set_data_payload("z", 1)
        store.set_data_payload("a", 2)
        store.set_data_payload("m", 3)
        assert store.list_data_payload_keys() == ["a", "m", "z"]

    def test_data_payload_property_returns_copy(self):
        store = EntityStore()
        store.set_data_payload("x", 1)
        snapshot = store.data_payload
        snapshot["x"] = 999
        assert store.get_data_payload("x") == 1

    def test_clear_removes_entities_and_payloads(self):
        store = EntityStore()
        store.add_entities([_make_entity("e1")])
        store.set_data_payload("key", "value")
        store.clear()
        assert store.entities == []
        assert store.list_data_payload_keys() == []
        assert store.get_data_payload("key") is None


class TestEntityStoreCache:
    def test_add_entities_avoids_duplicates(self):
        store = EntityStore()
        store.add_entities([_make_entity("e1"), _make_entity("e1")])
        assert len(store.entities) == 1

    def test_cache_get_many_returns_cached_entities(self):
        store = EntityStore()
        entity = _make_entity("e1")
        store.add_entities([entity])
        assert store.cache_get_many(["e1"], language="en") == [entity]

    def test_cache_misses_returns_uncached_ids(self):
        store = EntityStore()
        store.add_entities([_make_entity("e1")])
        assert store.cache_misses(["e1", "e2"], language="en") == ["e2"]

    def test_invalidate_ids_removes_entities_and_cache(self):
        store = EntityStore()
        store.add_entities([_make_entity("e1")])
        store.invalidate_ids(["e1"])
        assert store.entities == []
        assert store.cache_get_many(["e1"], language="en") == []
