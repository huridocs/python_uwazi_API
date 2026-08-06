import os
from datetime import datetime
from dotenv import load_dotenv

from uwazi_api.client import UwaziClient
from uwazi_api.domain.stats import SearchStats, TemplateStat, ThesaurusValueStat


load_dotenv()

UWAZI_USER = os.getenv("UWAZI_USER", "admin")
UWAZI_PASSWORD = os.getenv("UWAZI_PASSWORD", "admin")
UWAZI_URL = os.getenv("UWAZI_URL", "http://localhost:3000")


class TestStatsRepositoryE2E:
    @classmethod
    def setup_class(cls):
        cls.client = UwaziClient(user=UWAZI_USER, password=UWAZI_PASSWORD, url=UWAZI_URL)
        cls.stats_repo = cls.client.stats
        cls.template_repo = cls.client.templates
        cls.thesauri_repo = cls.client.thesauris

        templates = cls.template_repo.get()
        cls.has_templates = len(templates) > 0

    def test_01_get_stats_returns_search_stats(self):
        stats = self.stats_repo.get_stats()
        assert isinstance(stats, SearchStats)
        assert stats.total_entities >= 0
        assert isinstance(stats.templates, list)
        assert isinstance(stats.thesauri, list)

    def test_02_template_stats_have_expected_fields(self):
        stats = self.stats_repo.get_stats()
        for ts in stats.templates:
            assert isinstance(ts, TemplateStat)
            assert ts.template_id
            assert ts.template_name
            assert isinstance(ts.count, int)
            assert ts.count >= 0

    def test_03_template_names_resolved(self):
        if not self.has_templates:
            return
        stats = self.stats_repo.get_stats()
        templates = self.template_repo.get()
        template_map = {t.id: t.name for t in templates}
        for ts in stats.templates:
            if ts.template_id in template_map:
                assert ts.template_name == template_map[ts.template_id], (
                    f"Expected name '{template_map[ts.template_id]}' for template "
                    f"'{ts.template_id}', got '{ts.template_name}'"
                )

    def test_04_template_counts_sum_to_total(self):
        stats = self.stats_repo.get_stats()
        total_from_templates = sum(ts.count for ts in stats.templates)
        assert total_from_templates <= stats.total_entities

    def test_05_thesaurus_value_stats_have_expected_fields(self):
        stats = self.stats_repo.get_stats()
        for vs in stats.thesauri:
            assert isinstance(vs, ThesaurusValueStat)
            assert vs.thesaurus_id
            assert vs.thesaurus_name
            assert vs.value_id
            assert vs.value_label
            assert isinstance(vs.count, int)
            assert vs.count >= 0

    def test_06_get_stats_with_language(self):
        stats = self.stats_repo.get_stats(language="en")
        assert isinstance(stats, SearchStats)

    @classmethod
    def teardown_class(cls):
        pass


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v", "-s"])
