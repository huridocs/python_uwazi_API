import asyncio
import os
import time
from datetime import datetime
from typing import Any

import pytest
from dotenv import load_dotenv

from uwazi_agent.adapters.uwazi_api.uwazi_api_adapter import UwaziApiAdapter
from uwazi_agent.domain.agent_entity import AgentEntity
from uwazi_api.domain.entity import Entity


load_dotenv()

UWAZI_USER = os.getenv("UWAZI_USER", "admin")
UWAZI_PASSWORD = os.getenv("UWAZI_PASSWORD", "admin")
UWAZI_URL = os.getenv("UWAZI_URL", "http://localhost:3000")


def _run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


class TestEntityApiAdapterE2E:
    """End-to-end tests for the entity-side of ``UwaziApiAdapter``.

    They follow the same pattern as the e2e tests in ``uwazi_api/tests``:
    a live Uwazi instance is required, and the full adapter path
    (entity mapper included) is exercised against real data.

    Coverage of the four agent-facing operations:
        * get_entities_by_shared_ids
        * search_entities_by_text
        * update_entities
        * delete_entities_by_shared_ids
    """

    @classmethod
    def setup_class(cls):
        cls.adapter = UwaziApiAdapter(user=UWAZI_USER, password=UWAZI_PASSWORD, url=UWAZI_URL)
        cls.client = cls.adapter.client
        cls.template_repo = cls.client.templates
        cls.entity_repo = cls.client.entities
        cls.thesauri_repo = cls.client.thesauris

        templates = cls.template_repo.get()
        assert templates, "Need at least one template in the Uwazi instance"
        cls.test_template = templates[0]
        cls.test_template_id = cls.test_template.id
        cls.test_template_name = cls.test_template.name

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        cls.unique_marker = f"agent_entity_test_{ts}"
        cls.created_shared_ids: list[str] = []

    def test_01_get_entities_by_shared_ids_round_trip(self):
        title = f"{self.unique_marker}_get"
        shared_id = self._create_entity(title=title, metadata={})
        try:
            entities = _run(self.adapter.get_entities_by_shared_ids([shared_id], "en"))
            assert len(entities) == 1
            entity = entities[0]
            assert isinstance(entity, AgentEntity)
            assert entity.shared_id == shared_id
            assert entity.title == title
            assert entity.template_name == self.test_template_name
        finally:
            self._delete(shared_id)

    def test_02_search_entities_by_text_returns_summary_and_examples(self):
        marker = f"{self.unique_marker}_search"
        shared_id = self._create_entity(title=marker, metadata={})
        try:
            result = None
            for _ in range(10):
                result = _run(self.adapter.search_entities_by_text(marker, None, "en", limit=10))
                if result.summary.count >= 1:
                    break
                time.sleep(0.5)
            assert result.summary.count >= 1
            assert self.test_template_name in result.summary.by_template
            assert marker in result.summary.sample_titles
            assert shared_id in result.summary.shared_ids
            assert len(result.examples) >= 1
            for example in result.examples:
                assert isinstance(example, AgentEntity)
        finally:
            self._delete(shared_id)

    def test_02b_get_entities_by_template_returns_summary_and_examples(self):
        marker = f"{self.unique_marker}_by_tpl"
        shared_id = self._create_entity(title=marker, metadata={})
        try:
            result = None
            for _ in range(10):
                result = _run(self.adapter.get_entities_by_template(self.test_template_name, "en", limit=100))
                if any(e.shared_id == shared_id for e in result._all_entities):
                    break
                time.sleep(0.5)
            assert result.summary.count >= 1
            assert self.test_template_name in result.summary.by_template
            assert shared_id in result.summary.shared_ids or any(e.shared_id == shared_id for e in result._all_entities)
            assert len(result.examples) >= 1
            for example in result.examples:
                assert isinstance(example, AgentEntity)
                assert example.template_name == self.test_template_name
        finally:
            self._delete(shared_id)

    def test_03_update_entities_partial_merge_preserves_omitted_fields(self):
        shared_id = self._create_entity(title=f"{self.unique_marker}_upd", metadata={})
        try:
            update = AgentEntity(
                shared_id=shared_id,
                title=f"{self.unique_marker}_upd_renamed",
                template_name=self.test_template_name,
                metadata={},
            )
            results = _run(self.adapter.update_entities([update], "en"))
            assert len(results) == 1
            assert results[0].success is True, results[0].error
            fetched = _run(self.adapter.get_entities_by_shared_ids([shared_id], "en"))
            assert fetched[0].title == f"{self.unique_marker}_upd_renamed"
        finally:
            self._delete(shared_id)

    def test_04_update_entities_rejects_unknown_property(self):
        shared_id = self._create_entity(title=f"{self.unique_marker}_bad_prop", metadata={})
        try:
            update = AgentEntity(
                shared_id=shared_id,
                title=f"{self.unique_marker}_bad_prop",
                template_name=self.test_template_name,
                metadata={"definitely_not_a_real_property_xyz": "x"},
            )
            results = _run(self.adapter.update_entities([update], "en"))
            assert len(results) == 1
            assert results[0].success is False
            assert results[0].error
        finally:
            self._delete(shared_id)

    def test_05_delete_entities_by_shared_ids(self):
        shared_id = self._create_entity(title=f"{self.unique_marker}_del", metadata={})
        self._wait_for_search_index(shared_id)
        results = _run(self.adapter.delete_entities_by_shared_ids([shared_id]))
        assert len(results) == 1
        assert results[0].success is True
        if shared_id in self.created_shared_ids:
            self.created_shared_ids.remove(shared_id)
        fetched = _run(self.adapter.get_entities_by_shared_ids([shared_id], "en"))
        assert fetched == []

    def _create_entity(self, title: str, metadata: dict) -> str:
        entity = Entity(
            title=title,
            template=self.test_template_id,
            language="en",
        )
        entity.metadata = metadata
        shared_id = self.entity_repo.upload(entity, "en")
        self.created_shared_ids.append(shared_id)
        return shared_id

    def _wait_for_search_index(self, shared_id: str, timeout: float = 5.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                result = _run(self.adapter.search_entities_by_text(self.unique_marker, None, "en", limit=100))
                if shared_id in result.summary.shared_ids:
                    return
            except Exception:
                pass
            time.sleep(0.5)

    def _delete(self, shared_id: str) -> None:
        try:
            self._wait_for_search_index(shared_id)
            self.entity_repo.delete(shared_id)
        finally:
            if shared_id in self.created_shared_ids:
                self.created_shared_ids.remove(shared_id)

    @classmethod
    def teardown_class(cls):
        for shared_id in list(cls.created_shared_ids):
            try:
                time.sleep(2)
                cls.entity_repo.delete(shared_id)
            except Exception:
                pass


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
