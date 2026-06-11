import asyncio
import os
from datetime import datetime
from typing import Any

import pytest
from dotenv import load_dotenv

from uwazi_agent.adapters.uwazi_api.uwazi_api_adapter import UwaziApiAdapter
from uwazi_api.domain.menu_link import MenuLink


load_dotenv()

UWAZI_USER = os.getenv("UWAZI_USER", "admin")
UWAZI_PASSWORD = os.getenv("UWAZI_PASSWORD", "admin")
UWAZI_URL = os.getenv("UWAZI_URL", "http://localhost:3000")


def _run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


class TestMenuLinksAdapterE2E:
    """End-to-end coverage of the menu-link side of ``UwaziApiAdapter``."""

    @classmethod
    def setup_class(cls):
        cls.adapter = UwaziApiAdapter(user=UWAZI_USER, password=UWAZI_PASSWORD, url=UWAZI_URL)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        cls.unique_marker = f"menu_link_adapter_test_{ts}"
        cls.snapshot: list[MenuLink] = _run(cls.adapter.get_menu_links())

    @classmethod
    def teardown_class(cls):
        try:
            _run(cls.adapter.set_menu_links(cls.snapshot))
        except Exception:
            pass

    def test_01_get_menu_links_round_trip(self):
        result = _run(self.adapter.get_menu_links())
        assert isinstance(result, list)
        for link in result:
            assert isinstance(link, MenuLink)
            assert link.title

    def test_02_set_menu_links_appends_entry(self):
        marker = f"{self.unique_marker}_appended"
        new_entry = MenuLink(title=marker, type="link", url="/page/kkuafbu3wll/welcome")
        _run(self.adapter.set_menu_links([*self.snapshot, new_entry]))
        fetched = _run(self.adapter.get_menu_links())
        titles = [link.title for link in fetched]
        assert marker in titles
        marker_entry = next(link for link in fetched if link.title == marker)
        assert marker_entry.url == "/page/kkuafbu3wll/welcome"

    def test_03_set_menu_links_replaces_list(self):
        marker = f"{self.unique_marker}_only"
        _run(self.adapter.set_menu_links([MenuLink(title=marker, type="link", url="/page/only")]))
        fetched = _run(self.adapter.get_menu_links())
        assert len(fetched) == 1
        assert fetched[0].title == marker


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
