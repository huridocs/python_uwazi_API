import os
from datetime import datetime

from dotenv import load_dotenv
from uwazi_api.client import UwaziClient
from uwazi_api.domain.template import Template
from uwazi_api.domain.property_schema import PropertySchema
from uwazi_api.domain.property_type import PropertyType

load_dotenv()

UWAZI_USER = os.getenv("UWAZI_USER", "admin")
UWAZI_PASSWORD = os.getenv("UWAZI_PASSWORD", "admin")
UWAZI_URL = os.getenv("UWAZI_URL", "http://localhost:3000")


class TestTemplateRepositoryE2E:
    """End-to-end tests for TemplateRepository using real Uwazi connection."""

    @classmethod
    def setup_class(cls):
        """Set up the client for all tests."""
        cls.client = UwaziClient(user=UWAZI_USER, password=UWAZI_PASSWORD, url=UWAZI_URL)
        cls.template_repo = cls.client.templates
        cls.test_template_name = f"test_template_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        cls.created_template_id = None

    def test_01_get_all_templates(self):
        """Test get() method to retrieve all templates."""
        templates = self.template_repo.get()
        assert isinstance(templates, list)
        assert len(templates) > 0
        for template in templates:
            assert isinstance(template, Template)
            assert template.id is not None
            assert template.name is not None

    def test_02_get_by_id(self):
        """Test get_by_id() method."""
        templates = self.template_repo.get()
        assert len(templates) > 0
        first_template = templates[0]
        retrieved = self.template_repo.get_by_id(str(first_template.id))
        assert retrieved is not None
        assert retrieved.id == first_template.id
        assert retrieved.name == first_template.name

    def test_03_get_by_name(self):
        """Test get_by_name() method."""
        templates = self.template_repo.get()
        assert len(templates) > 0
        first_template = templates[0]
        retrieved = self.template_repo.get_by_name(first_template.name)
        assert retrieved is not None
        assert retrieved.id == first_template.id
        assert retrieved.name == first_template.name

    def test_04_get_by_nonexistent_id(self):
        """Test get_by_id() with nonexistent ID."""
        result = self.template_repo.get_by_id("nonexistent_id_12345")
        assert result is None

    def test_05_get_by_nonexistent_name(self):
        """Test get_by_name() with nonexistent name."""
        result = self.template_repo.get_by_name("nonexistent_template_12345")
        assert result is None

    def test_06_resolve_template_id_by_id(self):
        """Test resolve_template_id() with template ID."""
        templates = self.template_repo.get()
        assert len(templates) > 0
        first_template = templates[0]
        resolved_id = self.template_repo.resolve_template_id(first_template.id)
        assert resolved_id == first_template.id

    def test_07_resolve_template_id_by_name(self):
        """Test resolve_template_id() with template name."""
        templates = self.template_repo.get()
        assert len(templates) > 0
        first_template = templates[0]
        resolved_id = self.template_repo.resolve_template_id(first_template.name)
        assert resolved_id == first_template.id

    def test_08_resolve_nonexistent_template_id(self):
        """Test resolve_template_id() with nonexistent name/id."""
        result = self.template_repo.resolve_template_id("nonexistent_12345")
        assert result is None

    def test_09_find_property_in_template(self):
        """Test find_property() method."""
        templates = self.template_repo.get()
        assert len(templates) > 0
        template = templates[0]
        all_props = template.properties + template.common_properties
        if len(all_props) > 0:
            prop_name = all_props[0].name
            found_prop = self.template_repo.find_property(template.id, prop_name)
            assert found_prop is not None
            assert found_prop.name == prop_name

    def test_10_find_nonexistent_property(self):
        """Test find_property() with nonexistent property name."""
        templates = self.template_repo.get()
        assert len(templates) > 0
        template = templates[0]
        result = self.template_repo.find_property(template.id, "nonexistent_property_12345")
        assert result is None

    def test_11_find_property_by_template_name(self):
        """Test find_property() using template name instead of ID."""
        templates = self.template_repo.get()
        assert len(templates) > 0
        template = templates[0]
        all_props = template.properties + template.common_properties
        if len(all_props) > 0:
            prop_name = all_props[0].name
            found_prop = self.template_repo.find_property(template.name, prop_name)
            assert found_prop is not None
            assert found_prop.name == prop_name

    def test_12_ensure_property_filterable(self):
        """Test ensure_property_filterable() method."""
        templates = self.template_repo.get()
        assert len(templates) > 0
        template = templates[0]
        all_props = template.properties + template.common_properties
        filterable_props = [p for p in all_props if p.filter]
        if len(filterable_props) > 0:
            prop = filterable_props[0]
            try:
                self.template_repo.ensure_property_filterable(prop, prop.name)
            except Exception as e:
                assert False, f"ensure_property_filterable raised an exception: {e}"

    def test_13_create_template(self):
        """Test set() method to create a new template."""
        test_property = PropertySchema(
            name="test_text_field",
            label="Test Text Field",
            type=PropertyType.TEXT,
            filter=True,
        )
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
        new_template = Template(
            name=self.test_template_name,
            entityViewPage="",
            properties=[test_property],
            common_properties=common_props,
            color="#FF0000",
        )
        result = self.template_repo.set(language="en", template=new_template)
        assert result is not None
        assert "response" in result or "_id" in result or "name" in result
        if "_id" in result:
            self.__class__.created_template_id = result["_id"]
        else:
            self.template_repo.clear_cache()
            templates = self.template_repo.get()
            created = next((t for t in templates if t.name == self.test_template_name), None)
            assert created is not None
            self.__class__.created_template_id = created.id

    def test_14_verify_created_template_exists(self):
        """Verify the template was created and can be retrieved."""
        assert self.created_template_id is not None
        self.template_repo.clear_cache()
        retrieved = self.template_repo.get_by_id(self.created_template_id)
        assert retrieved is not None
        assert retrieved.name == self.test_template_name

    def test_15_verify_created_template_by_name(self):
        """Verify the template can be retrieved by name."""
        retrieved = self.template_repo.get_by_name(self.test_template_name)
        assert retrieved is not None
        assert retrieved.id == self.created_template_id

    def test_16_resolve_created_template_id(self):
        """Test resolve_template_id() with created template."""
        resolved_id = self.template_repo.resolve_template_id(self.test_template_name)
        assert resolved_id == self.created_template_id
        resolved_id = self.template_repo.resolve_template_id(self.created_template_id)
        assert resolved_id == self.created_template_id

    def test_17_find_property_in_created_template(self):
        """Test find_property() in the created template."""
        found_prop = self.template_repo.find_property(self.created_template_id, "test_text_field")
        assert found_prop is not None
        assert found_prop.name == "test_text_field"
        assert found_prop.type == PropertyType.TEXT

    def test_18_clear_cache_and_verify(self):
        """Test clear_cache() method."""
        self.template_repo.clear_cache()
        templates_after_clear = self.template_repo.get()
        assert isinstance(templates_after_clear, list)
        assert len(templates_after_clear) > 0

    def test_19_delete_created_template(self):
        """Test delete() method to remove the created template."""
        assert self.created_template_id is not None
        result = self.template_repo.delete(self.created_template_id)
        assert result is not None
        self.__class__.created_template_id = None

    def test_20_verify_template_deleted(self):
        """Verify the template was deleted."""
        if self.created_template_id:
            retrieved = self.template_repo.get_by_id(self.created_template_id)
            assert retrieved is None

    def test_21_cache_behavior(self):
        """Test that caching works correctly."""
        self.template_repo.clear_cache()
        templates1 = self.template_repo.get()
        templates2 = self.template_repo.get()
        assert templates1 == templates2
        assert templates1 is templates2


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v", "-s"])
