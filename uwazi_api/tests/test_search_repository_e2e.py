import os
import time
from datetime import datetime
from typing import Optional
import pytest
import pandas as pd
from dotenv import load_dotenv

from uwazi_api.client import UwaziClient
from uwazi_api.domain.entity import Entity
from uwazi_api.domain.search_filters import SearchFilters, DateRange, SelectFilter


load_dotenv()

UWAZI_USER = os.getenv("UWAZI_USER", "admin")
UWAZI_PASSWORD = os.getenv("UWAZI_PASSWORD", "admin")
UWAZI_URL = os.getenv("UWAZI_URL", "http://localhost:3000")


class TestSearchRepositoryE2E:
    """End-to-end tests for SearchRepository using real Uwazi connection."""

    @classmethod
    def setup_class(cls):
        """Set up the client and create test entities for all tests."""
        cls.client = UwaziClient(user=UWAZI_USER, password=UWAZI_PASSWORD, url=UWAZI_URL)
        cls.search_repo = cls.client.search
        cls.entity_repo = cls.client.entities
        cls.template_repo = cls.client.templates

        templates = cls.template_repo.get()
        assert len(templates) > 0, "No templates found in Uwazi instance"
        cls.test_template = templates[0]
        cls.test_template_id = cls.test_template.id
        cls.test_template_name = cls.test_template.name

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        cls.test_entity_title = f"search_test_{timestamp}"
        cls.test_entity_2_title = f"search_test_2_{timestamp}"
        cls.test_unique_term = f"unique_search_term_{timestamp}"

        cls.test_entity = Entity(
            title=cls.test_entity_title,
            template=cls.test_template_id,
            language="en",
            published=False,
        )
        cls.test_shared_id = cls.entity_repo.upload(cls.test_entity, "en")
        assert cls.test_shared_id is not None

        cls.test_entity_2 = Entity(
            title=cls.test_entity_2_title,
            template=cls.test_template_id,
            language="en",
            published=True,
        )
        cls.test_shared_id_2 = cls.entity_repo.upload(cls.test_entity_2, "en")
        assert cls.test_shared_id_2 is not None

        time.sleep(2)

    def test_01_get_shared_ids_returns_list(self):
        """Test get_shared_ids() method returns a list of shared IDs."""
        shared_ids = self.search_repo.get_shared_ids(
            to_process_template=self.test_template_name, batch_size=10, unpublished=True
        )
        assert isinstance(shared_ids, list)
        assert len(shared_ids) > 0

    def test_02_get_shared_ids_includes_created_entity(self):
        """Test get_shared_ids() includes the newly created entity."""
        shared_ids = self.search_repo.get_shared_ids(
            to_process_template=self.test_template_name, batch_size=10, unpublished=True
        )
        assert self.test_shared_id in shared_ids

    def test_03_get_shared_ids_with_published_false(self):
        """Test get_shared_ids() with published=False returns only published."""
        shared_ids = self.search_repo.get_shared_ids(
            to_process_template=self.test_template_name, batch_size=10, unpublished=False
        )
        assert isinstance(shared_ids, list)

    def test_04_get_returns_list_of_entities(self):
        """Test get() method returns list of Entity objects."""
        entities = self.search_repo.get(start_from=0, batch_size=10)
        assert isinstance(entities, list)
        assert len(entities) > 0
        assert all(isinstance(e, Entity) for e in entities)

    def test_05_get_with_template_filter(self):
        """Test get() method filters by template name."""
        entities = self.search_repo.get(start_from=0, batch_size=10, template_name=self.test_template_name)
        assert isinstance(entities, list)
        assert all(e.template == self.test_template_id for e in entities)

    def test_06_get_with_published_true(self):
        """Test get() method with published=True returns only published."""
        entities = self.search_repo.get(start_from=0, batch_size=10, published=True)
        assert isinstance(entities, list)
        assert all(e.published is True for e in entities)

    def test_07_get_with_published_false(self):
        """Test get() method with published=False returns unpublished."""
        entities = self.search_repo.get(start_from=0, batch_size=10, published=False)
        assert isinstance(entities, list)

    def test_08_search_by_text_returns_entities(self):
        """Test search_by_text() method returns matching entities."""
        entities = self.search_repo.search_by_text(search_term=self.test_entity_title, start_from=0, batch_size=10)
        assert isinstance(entities, list)

    def test_09_search_by_text_with_template_filter(self):
        """Test search_by_text() filters by template."""
        entities = self.search_repo.search_by_text(
            search_term=self.test_entity_title, template_name=self.test_template_name, start_from=0, batch_size=10
        )
        assert isinstance(entities, list)

    def test_10_search_by_text_nonexistent_term(self):
        """Test search_by_text() with nonexistent term returns empty list."""
        entities = self.search_repo.search_by_text(
            search_term="completely_nonexistent_term_123456789", start_from=0, batch_size=10
        )
        assert isinstance(entities, list)
        assert len(entities) == 0

    def test_11_search_by_filter_returns_entities(self):
        """Test search_by_filter() method returns matching entities."""
        filters = SearchFilters()
        entities = self.search_repo.search_by_filter(
            filters=filters, template_name=self.test_template_name, start_from=0, batch_size=10
        )
        assert isinstance(entities, list)

    def test_12_search_by_filter_invalid_property_raises_error(self):
        """Test search_by_filter() raises error for non-filterable property."""
        from uwazi_api.domain.exceptions import SearchError

        filters = SearchFilters()
        filters.add("creationDate", DateRange(from_=datetime.now().date()))
        with pytest.raises(SearchError, match="not filterable"):
            self.search_repo.search_by_filter(
                filters=filters, template_name=self.test_template_name, start_from=0, batch_size=10
            )

    def test_13_search_by_filter_nonexistent_property_raises_error(self):
        """Test search_by_filter() raises error for nonexistent property."""
        from uwazi_api.domain.exceptions import SearchError

        filters = SearchFilters()
        filters.add("nonexistent_property_123", SelectFilter(values=["true"]))
        with pytest.raises(SearchError, match="not found in template"):
            self.search_repo.search_by_filter(
                filters=filters, template_name=self.test_template_name, start_from=0, batch_size=10
            )

    def test_14_search_by_filter_with_order_and_sort(self):
        """Test search_by_filter() with custom order and sort."""
        filters = SearchFilters()
        entities = self.search_repo.search_by_filter(
            filters=filters, template_name=self.test_template_name, start_from=0, batch_size=10, order="asc", sort="title"
        )
        assert isinstance(entities, list)

    def test_15_search_by_filter_to_dataframe_returns_dataframe(self):
        """Test search_by_filter_to_dataframe() returns a DataFrame."""
        filters = SearchFilters()
        df = self.search_repo.search_by_filter_to_dataframe(
            filters=filters, template_name=self.test_template_name, start_from=0, batch_size=10
        )
        assert isinstance(df, pd.DataFrame)

    def test_16_search_by_filter_to_dataframe_with_columns(self):
        """Test search_by_filter_to_dataframe() returns DataFrame with expected columns."""
        filters = SearchFilters()
        df = self.search_repo.search_by_filter_to_dataframe(
            filters=filters, template_name=self.test_template_name, start_from=0, batch_size=10
        )
        assert isinstance(df, pd.DataFrame)
        assert len(df) > 0

    def test_17_get_with_language_parameter(self):
        """Test get() method respects language parameter."""
        entities = self.search_repo.get(start_from=0, batch_size=10, language="en")
        assert isinstance(entities, list)

    def test_18_search_by_text_with_language(self):
        """Test search_by_text() respects language parameter."""
        entities = self.search_repo.search_by_text(
            search_term=self.test_entity_title, language="en", start_from=0, batch_size=10
        )
        assert isinstance(entities, list)

    def test_19_search_by_filter_with_language(self):
        """Test search_by_filter() respects language parameter."""
        filters = SearchFilters()
        entities = self.search_repo.search_by_filter(
            filters=filters, template_name=self.test_template_name, language="en", start_from=0, batch_size=10
        )
        assert isinstance(entities, list)

    @classmethod
    def teardown_class(cls):
        """Clean up test entities."""
        if cls.test_shared_id:
            try:
                cls.entity_repo.delete(cls.test_shared_id)
            except Exception:
                pass

        if cls.test_shared_id_2:
            try:
                cls.entity_repo.delete(cls.test_shared_id_2)
            except Exception:
                pass


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v", "-s"])
