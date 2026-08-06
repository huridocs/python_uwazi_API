import os
from datetime import datetime

import pytest
from dotenv import load_dotenv

from uwazi_api.client import UwaziClient
from uwazi_api.domain.menu_link import MenuLink


load_dotenv()

UWAZI_USER = os.getenv("UWAZI_USER", "admin")
UWAZI_PASSWORD = os.getenv("UWAZI_PASSWORD", "admin")
UWAZI_URL = os.getenv("UWAZI_URL", "http://localhost:3000")


class TestMenuLinksRepositoryE2E:
    """End-to-end tests for MenuLinksRepository using a real Uwazi instance.

    The /api/settings/links endpoint is a full-replace list, so every test
    snapshots the current list, runs its assertions, then restores the
    snapshot.
    """

    @classmethod
    def setup_class(cls):
        cls.client = UwaziClient(user=UWAZI_USER, password=UWAZI_PASSWORD, url=UWAZI_URL)
        cls.repo = cls.client.menu_links
        cls.snapshot: list[MenuLink] = cls.repo.get_all()
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        cls.unique_marker = f"menu_link_test_{ts}"

    @classmethod
    def teardown_class(cls):
        cls.repo.replace_all(cls.snapshot)

    def test_01_get_all_returns_a_list(self):
        result = self.repo.get_all()
        assert isinstance(result, list)
        for link in result:
            assert isinstance(link, MenuLink)
            assert link.title

    def test_02_replace_all_round_trip(self):
        marker = f"{self.unique_marker}_replace"
        new_links = [*self.snapshot, MenuLink(title=marker, type="link", url="/page/abc123/marker")]
        written = self.repo.replace_all(new_links)
        assert all(isinstance(link, MenuLink) for link in written)
        fetched = self.repo.get_all()
        titles = [link.title for link in fetched]
        assert marker in titles
        marker_entry = next(link for link in fetched if link.title == marker)
        assert marker_entry.type == "link"
        assert marker_entry.url == "/page/abc123/marker"

    def test_03_replace_all_overwrites_existing(self):
        first_marker = f"{self.unique_marker}_first"
        second_marker = f"{self.unique_marker}_second"
        self.repo.replace_all([*self.snapshot, MenuLink(title=first_marker, type="link", url="/page/a")])
        self.repo.replace_all([*self.snapshot, MenuLink(title=second_marker, type="link", url="/page/b")])
        fetched = self.repo.get_all()
        titles = [link.title for link in fetched]
        assert first_marker not in titles
        assert second_marker in titles


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
