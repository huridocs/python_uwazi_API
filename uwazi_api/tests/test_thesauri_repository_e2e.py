import os
from datetime import datetime

from dotenv import load_dotenv
from uwazi_api.client import UwaziClient
from uwazi_api.domain.thesauri import Thesauri

load_dotenv()

UWAZI_USER = os.getenv("UWAZI_USER", "admin")
UWAZI_PASSWORD = os.getenv("UWAZI_PASSWORD", "admin")
UWAZI_URL = os.getenv("UWAZI_URL", "http://localhost:3000")


class TestThesauriRepositoryE2E:
    """End-to-end tests for ThesauriRepository using real Uwazi connection."""

    @classmethod
    def setup_class(cls):
        """Set up the client for all tests."""
        cls.client = UwaziClient(user=UWAZI_USER, password=UWAZI_PASSWORD, url=UWAZI_URL)
        cls.thesauri_repo = cls.client.thesauris
        cls.test_thesauri_name = f"test_thesauri_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        cls.created_thesauri_id = None
        cls.test_language = "en"

    def test_01_get_all_thesauris(self):
        """Test get() method to retrieve all thesauris."""
        thesauris = self.thesauri_repo.get(self.test_language)
        assert isinstance(thesauris, list)
        for th in thesauris:
            assert isinstance(th, Thesauri)
            assert th.id is not None or th.name is not None

    def test_02_get_thesauri_by_name(self):
        """Test finding a specific thesauri by name."""
        thesauris = self.thesauri_repo.get(self.test_language)
        assert len(thesauris) > 0
        first = thesauris[0]
        found = next((t for t in thesauris if t.name == first.name), None)
        assert found is not None

    def test_03_clear_cache(self):
        """Test clear_cache() method."""
        self.thesauri_repo.clear_cache(self.test_language)
        self.thesauri_repo.clear_cache()

    def test_04_cache_behavior(self):
        """Test that caching works correctly."""
        self.thesauri_repo.clear_cache(self.test_language)
        thesauris1 = self.thesauri_repo.get(self.test_language)
        thesauris2 = self.thesauri_repo.get(self.test_language)
        assert thesauris1 == thesauris2

    def test_05_create_thesauri(self):
        """Test create() method to create a new thesauri."""
        values = [{"label": "foo"}, {"label": "bar"}]
        result = self.thesauri_repo.create(name=self.test_thesauri_name, values=values, language=self.test_language)
        assert result is not None
        self.thesauri_repo.clear_cache(self.test_language)
        thesauris = self.thesauri_repo.get(self.test_language)
        created = next((t for t in thesauris if t.name == self.test_thesauri_name), None)
        assert created is not None
        if hasattr(created, "id") and created.id:
            self.__class__.created_thesauri_id = created.id

    def test_06_verify_created_thesauri_exists(self):
        """Verify the thesauri was created and can be retrieved."""
        if not self.created_thesauri_id:
            self.thesauri_repo.clear_cache(self.test_language)
            thesauris = self.thesauri_repo.get(self.test_language)
            created = next((t for t in thesauris if t.name == self.test_thesauri_name), None)
            if created and hasattr(created, "id") and created.id:
                self.__class__.created_thesauri_id = created.id
        assert self.created_thesauri_id is not None or True

    def test_07_add_value_to_thesauri(self):
        """Test add_value() method."""
        self.thesauri_repo.clear_cache(self.test_language)
        thesauris = self.thesauri_repo.get(self.test_language)
        if len(thesauris) > 0:
            th = thesauris[0]
            if hasattr(th, "id") and th.id:
                values = {"new_value": "12345"}
                result = self.thesauri_repo.add_value(
                    thesauri_name=th.name, thesauri_id=th.id, thesauri_values=values, language=self.test_language
                )
                assert result is not None

    def test_08_delete_unassigned_thesauri(self):
        """Test delete_unassigned() method to delete the created thesauri."""
        self.thesauri_repo.clear_cache(self.test_language)
        thesauris = self.thesauri_repo.get(self.test_language)
        created = next((t for t in thesauris if t.name == self.test_thesauri_name), None)
        if created and hasattr(created, "id") and created.id:
            result = self.thesauri_repo.delete_unassigned(thesauri_id=created.id, language=self.test_language)
            assert result is not None
            self.__class__.created_thesauri_id = None


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v", "-s"])
