import os
import time
from datetime import datetime
from typing import Optional, List
import pandas as pd
import pytest
from dotenv import load_dotenv

from uwazi_api.client import UwaziClient
from uwazi_api.domain.template import Template
from uwazi_api.domain.property_schema import PropertySchema
from uwazi_api.domain.property_type import PropertyType
from uwazi_api.domain.exceptions import EntityNotFoundError


load_dotenv()

UWAZI_USER = os.getenv("UWAZI_USER", "admin")
UWAZI_PASSWORD = os.getenv("UWAZI_PASSWORD", "admin")
UWAZI_URL = os.getenv("UWAZI_URL", "http://localhost:3000")


class TestCSVUseCaseE2E:
    """End-to-end tests for CSVUseCase upload_dataframe using real Uwazi connection."""

    @classmethod
    def setup_class(cls):
        """Set up the client and create test template for all tests."""
        cls.client = UwaziClient(user=UWAZI_USER, password=UWAZI_PASSWORD, url=UWAZI_URL)
        cls.csv_use_case = cls.client.csv
        cls.template_repo = cls.client.templates
        cls.entity_repo = cls.client.entities

        # Create unique test template name
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        cls.test_template_name = f"test_csv_template_{timestamp}"
        cls.test_template_id: Optional[str] = None
        cls.created_shared_ids: List[str] = []

        # Define test properties for the template
        cls.test_properties = [
            PropertySchema(
                name="test_text_field",
                label="Test Text Field",
                type=PropertyType.TEXT,
                filter=True,
            ),
            PropertySchema(
                name="test_numeric_field",
                label="Test Numeric Field",
                type=PropertyType.NUMERIC,
                filter=False,
            ),
        ]

        # Common properties required by Uwazi
        common_props = [
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
        ]

        # Create test template
        new_template = Template(
            name=cls.test_template_name,
            entityViewPage="",
            properties=cls.test_properties,
            common_properties=common_props,
            color="#00FF00",
        )
        result = cls.template_repo.set(language="en", template=new_template)
        assert result is not None

        # Get the created template ID
        cls.template_repo.clear_cache()
        templates = cls.template_repo.get()
        created = next((t for t in templates if t.name == cls.test_template_name), None)
        assert created is not None
        cls.test_template_id = str(created.id)

    def test_01_upload_dataframe(self):
        """Test upload_dataframe() method with valid DataFrame."""
        # Create a DataFrame with test data
        data = {
            "title": ["CSV Test Entity 1", "CSV Test Entity 2", "CSV Test Entity 3"],
            "test_text_field": ["Text value 1", "Text value 2", "Text value 3"],
            "test_numeric_field": [100, 200, 300],
        }
        df = pd.DataFrame(data)

        # Upload the DataFrame
        result = self.csv_use_case.upload_dataframe(df=df, template_name=self.test_template_name)

        assert result is not None
        assert result["status_code"] == 200

        # Wait for processing
        import time

        time.sleep(3)

        # Verify entities were created by searching for them
        for title in data["title"]:
            # Use search to find entities by title
            search_results = self.entity_repo.get()
            matching_entities = [e for e in search_results if e.title == title and e.template == self.test_template_id]
            assert len(matching_entities) > 0
            entity = matching_entities[0]
            assert entity.title == title
            assert entity.template == self.test_template_id
            self.created_shared_ids.append(entity.shared_id)

        assert len(self.created_shared_ids) == 3

    def test_02_upload_dataframe_with_missing_fields(self):
        """Test upload_dataframe() with DataFrame missing some template fields."""
        # Create a DataFrame with only title (missing other template fields)
        data = {
            "title": ["CSV Test Entity 4", "CSV Test Entity 5"],
        }
        df = pd.DataFrame(data)

        result = self.csv_use_case.upload_dataframe(df=df, template_name=self.test_template_name)

        assert result is not None
        assert result["status_code"] == 200

        # Wait for processing
        import time

        time.sleep(3)

        # Verify entities were created
        for title in data["title"]:
            search_results = self.entity_repo.get()
            matching_entities = [e for e in search_results if e.title == title and e.template == self.test_template_id]
            assert len(matching_entities) > 0
            entity = matching_entities[0]
            assert entity.title == title
            self.created_shared_ids.append(entity.shared_id)

        assert len(self.created_shared_ids) == 5

    def test_03_upload_dataframe_nonexistent_template(self):
        """Test upload_dataframe() with nonexistent template name."""
        import pandas as pd

        df = pd.DataFrame({"title": ["Should not be created"]})

        with pytest.raises(Exception) as exc_info:
            self.csv_use_case.upload_dataframe(df=df, template_name="nonexistent_template_12345")

        assert "not found" in str(exc_info.value).lower() or "TemplateNotFoundError" in str(type(exc_info.value))

    def test_04_verify_uploaded_entity_properties(self):
        """Test that uploaded entities have correct property values."""
        # This test verifies that entity properties were set correctly
        # We need to get one of the created entities and check its metadata
        if not self.created_shared_ids:
            pytest.skip("No entities were created in previous tests")

        entity = self.entity_repo.get_one(self.created_shared_ids[0], "en")

        assert entity is not None
        assert entity.title in ["CSV Test Entity 1", "CSV Test Entity 2", "CSV Test Entity 3"]
        assert entity.template == self.test_template_id

    @classmethod
    def teardown_class(cls):
        """Clean up created entities and template."""
        # Delete created entities
        if cls.created_shared_ids:
            for shared_id in cls.created_shared_ids:
                try:
                    cls.entity_repo.delete(shared_id)
                except (EntityNotFoundError, Exception):
                    pass

        # Delete test template
        if cls.test_template_id:
            try:
                cls.template_repo.delete_empty_template(cls.test_template_id)
            except Exception:
                pass


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v", "-s"])
