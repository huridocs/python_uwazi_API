import asyncio
import os
from datetime import datetime
from typing import Any

import pytest
from dotenv import load_dotenv

from uwazi_agent.adapters.uwazi_api.uwazi_api_adapter import UwaziApiAdapter
from uwazi_agent.domain.agent_page import AgentPage
from uwazi_agent.domain.agent_page_create import AgentPageCreate
from uwazi_agent.domain.agent_page_summary import AgentPageSummary
from uwazi_agent.domain.agent_page_update import AgentPageUpdate


load_dotenv()

UWAZI_USER = os.getenv("UWAZI_USER", "admin")
UWAZI_PASSWORD = os.getenv("UWAZI_PASSWORD", "admin")
UWAZI_URL = os.getenv("UWAZI_URL", "http://localhost:3000")


def _run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


class TestPageApiAdapterE2E:
    """End-to-end tests for the page-side of ``UwaziApiAdapter``.

    A live Uwazi instance is required; the full adapter path (page mapper +
    PagesRepository) is exercised against real data. Each test cleans up the
    pages it creates.
    """

    @classmethod
    def setup_class(cls):
        cls.adapter = UwaziApiAdapter(user=UWAZI_USER, password=UWAZI_PASSWORD, url=UWAZI_URL)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        cls.unique_marker = f"agent_page_test_{ts}"
        cls.created_shared_ids: list[str] = []

    def _create(self, title: str, content: str = "# hi", javascript: str | None = None) -> str:
        results = _run(
            self.adapter.create_pages([AgentPageCreate(title=title, content=content, javascript=javascript)], "en")
        )
        assert len(results) == 1
        assert results[0].success is True, results[0].error
        shared_id = results[0].shared_id
        assert shared_id
        self.created_shared_ids.append(shared_id)
        return shared_id

    def _delete(self, shared_id: str) -> None:
        try:
            _run(self.adapter.delete_pages_by_shared_ids([shared_id], "en"))
        finally:
            if shared_id in self.created_shared_ids:
                self.created_shared_ids.remove(shared_id)

    def test_01_create_and_get_round_trip(self):
        title = f"{self.unique_marker}_get"
        shared_id = self._create(title=title, content="# Hello\n\nBody.", javascript="console.log('hi')")
        try:
            pages = _run(self.adapter.get_pages_by_shared_ids([shared_id], "en"))
            assert len(pages) == 1
            page = pages[0]
            assert isinstance(page, AgentPage)
            assert page.shared_id == shared_id
            assert page.title == title
            assert "Hello" in page.content
            assert page.javascript == "console.log('hi')"
            assert page.url and shared_id in page.url
        finally:
            self._delete(shared_id)

    def test_02_list_pages_includes_created(self):
        title = f"{self.unique_marker}_list"
        shared_id = self._create(title=title, content="# listed")
        try:
            summaries = _run(self.adapter.list_pages("en"))
            assert all(isinstance(s, AgentPageSummary) for s in summaries)
            match = next((s for s in summaries if s.shared_id == shared_id), None)
            assert match is not None
            assert match.title == title
            assert match.has_markdown is True
        finally:
            self._delete(shared_id)

    def test_03_update_partial_merge(self):
        title = f"{self.unique_marker}_upd"
        shared_id = self._create(title=title, content="# original", javascript="var a = 1;")
        try:
            results = _run(self.adapter.update_pages([AgentPageUpdate(shared_id=shared_id, content="# updated body")], "en"))
            assert len(results) == 1
            assert results[0].success is True, results[0].error
            fetched = _run(self.adapter.get_pages_by_shared_ids([shared_id], "en"))
            assert "updated body" in fetched[0].content
            # javascript was not part of the update -> preserved
            assert fetched[0].javascript == "var a = 1;"
            # title was not part of the update -> preserved
            assert fetched[0].title == title
        finally:
            self._delete(shared_id)

    def test_04_update_unknown_shared_id_reports_failure(self):
        results = _run(
            self.adapter.update_pages([AgentPageUpdate(shared_id="definitely_not_a_real_page_xyz", content="x")], "en")
        )
        assert len(results) == 1
        assert results[0].success is False
        assert results[0].error

    def test_05_delete_pages(self):
        shared_id = self._create(title=f"{self.unique_marker}_del", content="# del")
        results = _run(self.adapter.delete_pages_by_shared_ids([shared_id], "en"))
        assert len(results) == 1
        assert results[0].success is True
        if shared_id in self.created_shared_ids:
            self.created_shared_ids.remove(shared_id)
        fetched = _run(self.adapter.get_pages_by_shared_ids([shared_id], "en"))
        assert fetched == []

    @classmethod
    def teardown_class(cls):
        for shared_id in list(cls.created_shared_ids):
            try:
                _run(cls.adapter.delete_pages_by_shared_ids([shared_id], "en"))
            except Exception:
                pass


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
