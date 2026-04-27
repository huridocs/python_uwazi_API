import os
import time
from datetime import date, datetime
from typing import Optional
import pytest
import pandas as pd
from dotenv import load_dotenv

from uwazi_api.client import UwaziClient
from uwazi_api.domain.entity import Entity
from uwazi_api.domain.search_filters import SearchFilters, DateRange, SelectFilter
from uwazi_api.domain.template import Template
from uwazi_api.domain.property_schema import PropertySchema
from uwazi_api.domain.property_type import PropertyType


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

        cls.filter_template_name = f"search_filter_test_{timestamp}"
        cls.filter_date_prop = f"filter_date_{timestamp}"
        cls.filter_select_prop = f"filter_select_{timestamp}"

        select_values = [{"label": "option1"}, {"label": "option2"}, {"label": "option3"}]
        thesauri_result = cls.client.thesauris.create(name=f"test_select_{timestamp}", values=select_values, language="en")
        cls.filter_thesauri_id = None
        if "_id" in thesauri_result:
            cls.filter_thesauri_id = thesauri_result["_id"]
        else:
            cls.client.thesauris.clear_cache("en")
            thesauris = cls.client.thesauris.get("en")
            created = next((t for t in thesauris if t.name == f"test_select_{timestamp}"), None)
            if created and hasattr(created, "id") and created.id:
                cls.filter_thesauri_id = created.id

        filter_template = Template(
            name=cls.filter_template_name,
            properties=[
                PropertySchema(
                    name=cls.filter_date_prop,
                    label="Filter Date",
                    type=PropertyType.DATE,
                    filter=True,
                ),
                PropertySchema(
                    name=cls.filter_select_prop,
                    label="Filter Select",
                    type=PropertyType.SELECT,
                    filter=True,
                    content=cls.filter_thesauri_id,
                ),
            ],
            common_properties=[
                PropertySchema(
                    name="title",
                    label="Title",
                    type=PropertyType.TEXT,
                    required=True,
                    isCommonProperty=True,
                ),
                PropertySchema(
                    name="creationDate",
                    label="Creation Date",
                    type=PropertyType.DATE,
                    isCommonProperty=True,
                ),
                PropertySchema(
                    name="editDate",
                    label="Edit Date",
                    type=PropertyType.DATE,
                    isCommonProperty=True,
                ),
            ],
        )
        filter_result = cls.template_repo.set("en", filter_template)
        cls.filter_template_id = filter_result.get("_id") if isinstance(filter_result, dict) else filter_result.id
        assert cls.filter_template_id is not None

        cls.template_repo.clear_cache()
        created_filter_template = cls.template_repo.get_by_id(cls.filter_template_id)
        cls.filter_date_prop = created_filter_template.properties[0].name
        cls.filter_select_prop = created_filter_template.properties[1].name

        test_date = date(2024, 1, 15)
        cls.test_entity = Entity(
            title=cls.test_entity_title,
            template=cls.filter_template_id,
            language="en",
            published=False,
        )
        cls.test_entity.metadata = {
            cls.filter_date_prop: test_date,
            cls.filter_select_prop: "option1",
        }
        cls.test_shared_id = cls.entity_repo.upload(cls.test_entity, "en")
        assert cls.test_shared_id is not None

        cls.test_entity_2 = Entity(
            title=cls.test_entity_2_title,
            template=cls.filter_template_id,
            language="en",
            published=True,
        )
        cls.test_entity_2.metadata = {
            cls.filter_date_prop: date(2024, 6, 20),
            cls.filter_select_prop: "option2",
        }
        cls.test_shared_id_2 = cls.entity_repo.upload(cls.test_entity_2, "en")
        assert cls.test_shared_id_2 is not None

        time.sleep(2)

    def test_01_get_shared_ids_returns_list(self):
        """Test get_shared_ids() method returns a list of shared IDs."""
        shared_ids = self.search_repo.get_shared_ids(
            to_process_template=self.filter_template_name, batch_size=10, unpublished=True
        )
        assert isinstance(shared_ids, list)
        assert len(shared_ids) > 0

    def test_02_get_shared_ids_includes_created_entity(self):
        """Test get_shared_ids() includes the newly created entity."""
        shared_ids = self.search_repo.get_shared_ids(
            to_process_template=self.filter_template_name, batch_size=10, unpublished=True
        )
        assert self.test_shared_id in shared_ids

    def test_03_get_shared_ids_with_published_false(self):
        """Test get_shared_ids() with published=False returns only published."""
        shared_ids = self.search_repo.get_shared_ids(
            to_process_template=self.filter_template_name, batch_size=10, unpublished=False
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
        entities = self.search_repo.get(start_from=0, batch_size=10, template_name=self.filter_template_name)
        assert isinstance(entities, list)
        assert all(e.template == self.filter_template_id for e in entities)

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
            search_term=self.test_entity_title, template_name=self.filter_template_name, start_from=0, batch_size=10
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
            filters=filters, template_name=self.filter_template_name, start_from=0, batch_size=10
        )
        assert isinstance(entities, list)

    def test_12_search_by_filter_invalid_property_raises_error(self):
        """Test search_by_filter() raises error for non-filterable property."""
        from uwazi_api.domain.exceptions import SearchError

        filters = SearchFilters()
        filters.add("creationDate", DateRange(from_=datetime.now().date()))
        with pytest.raises(SearchError, match="not filterable"):
            self.search_repo.search_by_filter(
                filters=filters, template_name=self.filter_template_name, start_from=0, batch_size=10
            )

    def test_13_search_by_filter_nonexistent_property_raises_error(self):
        """Test search_by_filter() raises error for nonexistent property."""
        from uwazi_api.domain.exceptions import SearchError

        filters = SearchFilters()
        filters.add("nonexistent_property_123", SelectFilter(values=["true"]))
        with pytest.raises(SearchError, match="not found in template"):
            self.search_repo.search_by_filter(
                filters=filters, template_name=self.filter_template_name, start_from=0, batch_size=10
            )

    def test_14_search_by_filter_with_order_and_sort(self):
        """Test search_by_filter() with custom order and sort."""
        filters = SearchFilters()
        entities = self.search_repo.search_by_filter(
            filters=filters, template_name=self.filter_template_name, start_from=0, batch_size=10, order="asc", sort="title"
        )
        assert isinstance(entities, list)

    def test_15_search_by_filter_to_dataframe_returns_dataframe(self):
        """Test search_by_filter_to_dataframe() returns a DataFrame."""
        filters = SearchFilters()
        df = self.search_repo.search_by_filter_to_dataframe(
            filters=filters, template_name=self.filter_template_name, start_from=0, batch_size=10
        )
        assert isinstance(df, pd.DataFrame)

    def test_16_search_by_filter_to_dataframe_with_columns(self):
        """Test search_by_filter_to_dataframe() returns DataFrame with expected columns."""
        filters = SearchFilters()
        df = self.search_repo.search_by_filter_to_dataframe(
            filters=filters, template_name=self.filter_template_name, start_from=0, batch_size=10
        )
        assert isinstance(df, pd.DataFrame)
        assert len(df) > 0

    def test_20_search_by_filter_with_date_filter(self):
        """Test search_by_filter() with date range filter."""
        filters = SearchFilters()
        filters.add(self.filter_date_prop, DateRange(from_=date(2024, 1, 1), to=date(2024, 12, 31)))
        entities = self.search_repo.search_by_filter(
            filters=filters, template_name=self.filter_template_name, start_from=0, batch_size=10
        )
        assert isinstance(entities, list)
        assert len(entities) >= 2

    def test_21_search_by_filter_with_select_filter(self):
        """Test search_by_filter() with select filter."""
        filters = SearchFilters()
        filters.add(self.filter_select_prop, SelectFilter(values=["option1"]))
        entities = self.search_repo.search_by_filter(
            filters=filters, template_name=self.filter_template_name, start_from=0, batch_size=10
        )
        assert isinstance(entities, list)
        assert len(entities) >= 1

    def test_22_search_by_filter_with_combined_date_and_select(self):
        """Test search_by_filter() with both date and select filters."""
        filters = SearchFilters()
        filters.add(self.filter_date_prop, DateRange(from_=date(2024, 1, 1)))
        filters.add(self.filter_select_prop, SelectFilter(values=["option1"]))
        entities = self.search_repo.search_by_filter(
            filters=filters, template_name=self.filter_template_name, start_from=0, batch_size=10
        )
        assert isinstance(entities, list)
        assert len(entities) >= 1

    def test_23_search_by_filter_with_date_only_option2(self):
        """Test search_by_filter() with date filter finding option2 entities."""
        filters = SearchFilters()
        filters.add(self.filter_date_prop, DateRange(from_=date(2024, 6, 1), to=date(2024, 12, 31)))
        filters.add(self.filter_select_prop, SelectFilter(values=["option2"]))
        entities = self.search_repo.search_by_filter(
            filters=filters, template_name=self.filter_template_name, start_from=0, batch_size=10
        )
        assert isinstance(entities, list)
        assert len(entities) >= 1

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
            filters=filters, template_name=self.filter_template_name, language="en", start_from=0, batch_size=10
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

        if hasattr(cls, "filter_template_id") and cls.filter_template_id:
            try:
                cls.template_repo.delete_empty_template(cls.filter_template_id)
            except Exception:
                pass

        if hasattr(cls, "filter_thesauri_id") and cls.filter_thesauri_id:
            try:
                cls.client.thesauris.delete_unassigned(cls.filter_thesauri_id, "en")
            except Exception:
                pass


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v", "-s"])
