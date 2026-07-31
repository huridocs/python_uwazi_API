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


class TestThesauriFromDataframeE2E:
    """End-to-end tests for ThesauriFromDataframeUseCase using real Uwazi connection."""

    @classmethod
    def setup_class(cls):
        """Set up the client and create test template with select properties."""
        cls.client = UwaziClient(user=UWAZI_USER, password=UWAZI_PASSWORD, url=UWAZI_URL)
        cls.template_repo = cls.client.templates
        cls.thesauri_repo = cls.client.thesauris
        cls.thesauri_from_df = cls.client.thesauri_from_df

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        cls.test_template_name = f"test_thesauri_df_{timestamp}"
        cls.thesauri_name_1 = f"test_select_t_{timestamp}"
        cls.thesauri_name_2 = f"test_multiselect_t_{timestamp}"
        cls.test_template_id: Optional[str] = None
        cls.thesauri_1_id: Optional[str] = None
        cls.thesauri_2_id: Optional[str] = None
        cls.created_shared_ids: List[str] = []

        cls.thesauri_repo.clear_cache()

        th_1 = cls.thesauri_repo.create(name=cls.thesauri_name_1, values=[], language="en")
        cls.thesauri_1_id = th_1["_id"]

        th_2 = cls.thesauri_repo.create(name=cls.thesauri_name_2, values=[], language="en")
        cls.thesauri_2_id = th_2["_id"]

        cls.test_properties = [
            PropertySchema(
                name="test_text_field",
                label="Test Text Field",
                type=PropertyType.TEXT,
                filter=True,
            ),
            PropertySchema(
                name="test_select_prop",
                label="Test Select Prop",
                type=PropertyType.SELECT,
                content=cls.thesauri_1_id,
                filter=True,
            ),
            PropertySchema(
                name="test_multiselect_prop",
                label="Test Multiselect Prop",
                type=PropertyType.MULTI_SELECT,
                content=cls.thesauri_2_id,
                filter=True,
            ),
        ]

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

        new_template = Template(
            name=cls.test_template_name,
            entityViewPage="",
            properties=cls.test_properties,
            common_properties=common_props,
            color="#FF0000",
        )
        result = cls.template_repo.set(language="en", template=new_template)
        assert result is not None

        cls.template_repo.clear_cache()
        templates = cls.template_repo.get()
        created = next((t for t in templates if t.name == cls.test_template_name), None)
        assert created is not None
        cls.test_template_id = str(created.id)

    def test_01_add_new_thesauri_values_from_dataframe(self):
        """Test that new values from dataframe are added to thesauris."""
        data = {
            "title": ["Entity A", "Entity B"],
            "test_select_prop": ["Red", "Blue"],
            "test_multiselect_prop": ["Large", "Small"],
        }
        df = pd.DataFrame(data)

        self.thesauri_repo.clear_cache("en")
        results = self.thesauri_from_df.execute(df=df, template_name=self.test_template_name, language="en")

        assert "test_select_prop" in results
        assert "test_multiselect_prop" in results

        self.thesauri_repo.clear_cache("en")
        th_1 = self.thesauri_repo.get("en")
        th1_obj = next((t for t in th_1 if t.id == self.thesauri_1_id), None)
        assert th1_obj is not None
        th1_labels = {v.label for v in th1_obj.values}
        assert "Red" in th1_labels
        assert "Blue" in th1_labels

        th_2 = self.thesauri_repo.get("en")
        th2_obj = next((t for t in th_2 if t.id == self.thesauri_2_id), None)
        assert th2_obj is not None
        th2_labels = {v.label for v in th2_obj.values}
        assert "Large" in th2_labels
        assert "Small" in th2_labels

    def test_02_no_duplicate_values_on_re_run(self):
        """Test that running again doesn't duplicate existing values."""
        data = {
            "title": ["Entity C", "Entity C"],
            "test_select_prop": ["Red", "Green"],
            "test_multiselect_prop": ["Large", "Large"],
        }
        df = pd.DataFrame(data)

        self.thesauri_repo.clear_cache("en")
        results = self.thesauri_from_df.execute(df=df, template_name=self.test_template_name, language="en")

        self.thesauri_repo.clear_cache("en")
        th_1 = self.thesauri_repo.get("en")
        th1_obj = next((t for t in th_1 if t.id == self.thesauri_1_id), None)
        assert th1_obj is not None
        th1_labels = [v.label for v in th1_obj.values]
        assert th1_labels.count("Red") == 1
        assert "Green" in th1_labels

        th_2 = self.thesauri_repo.get("en")
        th2_obj = next((t for t in th_2 if t.id == self.thesauri_2_id), None)
        assert th2_obj is not None
        assert "Large" in [v.label for v in th2_obj.values]

    def test_03_no_changes_when_all_values_exist(self):
        """Test that no changes are made when all values already exist."""
        data = {
            "title": ["Entity D"],
            "test_select_prop": ["Red"],
            "test_multiselect_prop": ["Large"],
        }
        df = pd.DataFrame(data)

        self.thesauri_repo.clear_cache("en")
        results = self.thesauri_from_df.execute(df=df, template_name=self.test_template_name, language="en")

        assert results["test_select_prop"]["status"] == "no_new_values"
        assert results["test_multiselect_prop"]["status"] == "no_new_values"

    def test_04_nan_values_handled(self):
        """Test that NaN values in dataframe are skipped."""
        data = {
            "title": ["Entity E", "Entity E"],
            "test_select_prop": ["Green", None],
        }
        df = pd.DataFrame(data)

        self.thesauri_repo.clear_cache("en")
        results = self.thesauri_from_df.execute(df=df, template_name=self.test_template_name, language="en")

        assert "test_select_prop" in results
        th = self.thesauri_repo.get("en")
        th_obj = next((t for t in th if t.id == self.thesauri_1_id), None)
        assert th_obj is not None
        assert "Green" in [v.label for v in th_obj.values]

    def test_05_mixed_case_column_names(self):
        """Test handling of columns with spaces and mixed case."""
        data = {
            "Title": ["Entity F"],
            "test select prop": ["Yellow"],
            "Test Multiselect Prop": ["Medium"],
        }
        df = pd.DataFrame(data)

        self.thesauri_repo.clear_cache("en")
        results = self.thesauri_from_df.execute(df=df, template_name=self.test_template_name, language="en")

        assert "test_select_prop" in results
        self.thesauri_repo.clear_cache("en")
        th_1 = self.thesauri_repo.get("en")
        th1_obj = next((t for t in th_1 if t.id == self.thesauri_1_id), None)
        assert th1_obj is not None
        assert "Yellow" in [v.label for v in th1_obj.values]

    def test_06_template_not_found(self):
        """Test that ValueError is raised for nonexistent template."""
        df = pd.DataFrame({"title": ["Test"]})

        with pytest.raises(ValueError) as exc_info:
            self.thesauri_from_df.execute(df=df, template_name="nonexistent_template_xyz", language="en")

        assert "not found" in str(exc_info.value).lower()

    @classmethod
    def teardown_class(cls):
        """Clean up created entities and template."""
        if cls.created_shared_ids:
            for shared_id in cls.created_shared_ids:
                try:
                    cls.client.entities.delete(shared_id)
                except (EntityNotFoundError, Exception):
                    pass

        if cls.test_template_id:
            try:
                cls.template_repo.delete_empty_template(cls.test_template_id)
            except Exception:
                pass

        if cls.thesauri_1_id:
            try:
                cls.thesauri_repo.delete_unassigned(cls.thesauri_1_id, "en")
            except Exception:
                pass

        if cls.thesauri_2_id:
            try:
                cls.thesauri_repo.delete_unassigned(cls.thesauri_2_id, "en")
            except Exception:
                pass


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v", "-s"])
