import os
import time
from datetime import datetime
from typing import Optional, List, Dict
import pandas as pd
import pytest
from dotenv import load_dotenv

from uwazi_api.client import UwaziClient
from uwazi_api.domain.template import Template
from uwazi_api.domain.property_schema import PropertySchema
from uwazi_api.domain.property_type import PropertyType
from uwazi_api.domain.entity import Entity
from uwazi_api.domain.exceptions import EntityNotFoundError


load_dotenv()

UWAZI_USER = os.getenv("UWAZI_USER", "admin")
UWAZI_PASSWORD = os.getenv("UWAZI_PASSWORD", "admin")
UWAZI_URL = os.getenv("UWAZI_URL", "http://localhost:3000")


class TestEntityDataFrameE2E:
    """End-to-end tests for create_or_update_entities_from_dataframe using real Uwazi API."""

    @classmethod
    def setup_class(cls):
        """Set up the client and create test template with various property types."""
        cls.client = UwaziClient(user=UWAZI_USER, password=UWAZI_PASSWORD, url=UWAZI_URL)
        cls.entity_repo = cls.client.entities
        cls.template_repo = cls.client.templates
        cls.created_shared_ids: List[str] = []
        cls.property_name_map: Dict[str, str] = {}

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        cls.test_template_name = f"test_dataframe_template_{timestamp}"
        cls.test_template_id: Optional[str] = None

        # Step 1: Create relationship type first
        rel_type_name = f"test_rel_type_{timestamp}"
        rel_type_result = cls.client.relationships.create_relation_type(rel_type_name, language="en")
        print(f"Relationship type created: {rel_type_result}")
        time.sleep(2)

        cls.client.relationships.clear_cache()
        rel_type = cls.client.relationships.get_relation_type_by_name(rel_type_name)
        if not rel_type:
            raise Exception("Failed to create relationship type")
        cls.rel_type_id = rel_type.id
        print(f"Relationship type ID: {cls.rel_type_id}")

        # Step 2: Create template WITHOUT relationship property first
        cls.test_properties = [
            PropertySchema(name="test_text", label="Test Text", type=PropertyType.TEXT, filter=True),
            PropertySchema(name="test_numeric", label="Test Numeric", type=PropertyType.NUMERIC),
            PropertySchema(name="test_date", label="Test Date", type=PropertyType.DATE),
            PropertySchema(name="test_geolocation", label="Test Geolocation", type=PropertyType.GEO_LOCATION),
            PropertySchema(name="test_daterange", label="Test Daterange", type=PropertyType.DATE_RANGE),
            PropertySchema(name="test_multidate", label="Test Multidate", type=PropertyType.MULTI_DATE),
            PropertySchema(name="test_multidaterange", label="Test Multidaterange", type=PropertyType.MULTI_DATE_RANGE),
        ]

        common_props = [
            PropertySchema(name="title", label="Title", type=PropertyType.TEXT, required=True, isCommonProperty=True),
            PropertySchema(name="creationDate", label="Creation Date", type=PropertyType.DATE, isCommonProperty=True),
            PropertySchema(name="editDate", label="Edit Date", type=PropertyType.DATE, isCommonProperty=True),
        ]

        new_template = Template(
            name=cls.test_template_name,
            entityViewPage="",
            properties=cls.test_properties,
            common_properties=common_props,
            color="#00FF00",
        )
        result = cls.template_repo.set(language="en", template=new_template)
        print(f"Template creation result: {result}")

        time.sleep(3)
        cls.template_repo.clear_cache()
        templates = cls.template_repo.get()
        created = next((t for t in templates if t.name == cls.test_template_name), None)
        assert created is not None, f"Template '{cls.test_template_name}' not found after creation."
        cls.test_template_id = str(created.id)
        print(f"Test template ID: {cls.test_template_id}")

        # Step 3: NOW add relationship property with content = template ID
        relationship_prop = PropertySchema(
            name="test_relationship",
            label="Test Relationship",
            type=PropertyType.RELATIONSHIP,
            content=cls.test_template_id,  # Related entities must have this template
            relationType=cls.rel_type_id,  # Relationship type ID
        )
        created.properties.append(relationship_prop)

        # Update the template with the relationship property
        cls.template_repo.set(language="en", template=created)
        cls.template_repo.clear_cache()
        time.sleep(2)

        # Verify the template was updated correctly
        updated_template = cls.template_repo.get_by_id(cls.test_template_id)
        print(
            f"Updated template properties: {[(p.name, p.type, p.content, p.relationType) for p in updated_template.properties if p.type == 'relationship']}"
        )

        # Get actual property names from template (Uwazi may append type to name)
        for prop in updated_template.properties:
            cls.property_name_map[prop.name] = prop.name
            # Also map without type suffix if present
            if "_" in prop.name:
                parts = prop.name.rsplit("_", 1)
                if len(parts) == 2:
                    cls.property_name_map[parts[0]] = prop.name

        print(f"Property name mapping: {cls.property_name_map}")

    def test_01_upload_new_entities_from_dataframe(self):
        """Test creating new entities from DataFrame."""
        text_prop = self.property_name_map.get("test_text", "test_text")
        numeric_prop = self.property_name_map.get("test_numeric", "test_numeric")

        data = {
            "title": ["DF Test Entity 1", "DF Test Entity 2"],
            "template": [self.test_template_id, self.test_template_id],
            text_prop: ["Text value 1", "Text value 2"],
            numeric_prop: [100, 200],
        }
        df = pd.DataFrame(data)

        responses = self.entity_repo.create_or_update_entities_from_dataframe(df, language="en")

        assert len(responses) == 2
        for resp in responses:
            assert resp.success is True
            assert resp.shared_id is not None
            assert resp.entity is not None
            self.created_shared_ids.append(resp.shared_id)

        time.sleep(2)

        for shared_id in self.created_shared_ids[:2]:
            entity = self.entity_repo.get_one(shared_id, "en")
            assert entity.title in ["DF Test Entity 1", "DF Test Entity 2"]
            assert entity.template == self.test_template_id

    def test_02_update_existing_entities_from_dataframe(self):
        """Test updating existing entities from DataFrame using sharedId."""
        if not self.created_shared_ids:
            pytest.skip("No entities created yet")

        text_prop = self.property_name_map.get("test_text", "test_text")
        numeric_prop = self.property_name_map.get("test_numeric", "test_numeric")

        data = {
            "sharedId": [self.created_shared_ids[0]],
            "title": ["DF Test Entity 1 Updated"],
            "template": [self.test_template_id],
            text_prop: ["Updated text value"],
            numeric_prop: [999],
        }
        df = pd.DataFrame(data)

        responses = self.entity_repo.create_or_update_entities_from_dataframe(df, language="en")

        assert len(responses) == 1
        assert responses[0].success is True

        time.sleep(2)

        entity = self.entity_repo.get_one(self.created_shared_ids[0], "en")
        assert entity.title == "DF Test Entity 1 Updated"

    def test_03_upload_with_date_property(self):
        """Test uploading entities with date property."""
        date_prop = self.property_name_map.get("test_date", "test_date")

        data = {
            "title": ["DF Date Test Entity"],
            "template": [self.test_template_id],
            date_prop: ["2026/04/27"],
        }
        df = pd.DataFrame(data)

        responses = self.entity_repo.create_or_update_entities_from_dataframe(df, language="en")

        assert len(responses) == 1
        assert responses[0].success is True
        self.created_shared_ids.append(responses[0].shared_id)

        time.sleep(2)

        entity = self.entity_repo.get_one(responses[0].shared_id, "en")
        assert entity.title == "DF Date Test Entity"

    def test_04_upload_with_geolocation_property(self):
        """Test uploading entities with geolocation property."""
        geo_prop = self.property_name_map.get("test_geolocation", "test_geolocation")

        data = {
            "title": ["DF Geo Test Entity"],
            "template": [self.test_template_id],
            geo_prop: ["40.7128|-74.0060"],
        }
        df = pd.DataFrame(data)

        responses = self.entity_repo.create_or_update_entities_from_dataframe(df, language="en")

        assert len(responses) == 1
        assert responses[0].success is True
        self.created_shared_ids.append(responses[0].shared_id)

        time.sleep(2)

        entity = self.entity_repo.get_one(responses[0].shared_id, "en")
        assert entity.title == "DF Geo Test Entity"

    def test_05_upload_with_daterange_property(self):
        """Test uploading entities with daterange property."""
        dr_prop = self.property_name_map.get("test_daterange", "test_daterange")

        data = {
            "title": ["DF Daterange Test Entity"],
            "template": [self.test_template_id],
            dr_prop: ["2026/04/01:2026/04/30"],
        }
        df = pd.DataFrame(data)

        responses = self.entity_repo.create_or_update_entities_from_dataframe(df, language="en")

        assert len(responses) == 1
        assert responses[0].success is True
        self.created_shared_ids.append(responses[0].shared_id)

        time.sleep(2)

        entity = self.entity_repo.get_one(responses[0].shared_id, "en")
        assert entity.title == "DF Daterange Test Entity"

    def test_06_upload_with_multidate_property(self):
        """Test uploading entities with multidate property."""
        md_prop = self.property_name_map.get("test_multidate", "test_multidate")

        data = {
            "title": ["DF Multidate Test Entity"],
            "template": [self.test_template_id],
            md_prop: ["2026/04/01|2026/04/15|2026/04/30"],
        }
        df = pd.DataFrame(data)

        responses = self.entity_repo.create_or_update_entities_from_dataframe(df, language="en")

        assert len(responses) == 1
        assert responses[0].success is True
        self.created_shared_ids.append(responses[0].shared_id)

        time.sleep(2)

        entity = self.entity_repo.get_one(responses[0].shared_id, "en")
        assert entity.title == "DF Multidate Test Entity"

    def test_07_upload_with_documents(self):
        """Test uploading entities with documents field."""
        data = {
            "title": ["DF Documents Test Entity"],
            "template": [self.test_template_id],
            "documents": ["doc1.pdf|doc2.pdf"],
        }
        df = pd.DataFrame(data)

        responses = self.entity_repo.create_or_update_entities_from_dataframe(df, language="en")

        assert len(responses) == 1
        assert responses[0].success is True
        self.created_shared_ids.append(responses[0].shared_id)

    def test_08_upload_with_attachments(self):
        """Test uploading entities with attachments field."""
        data = {
            "title": ["DF Attachments Test Entity"],
            "template": [self.test_template_id],
            "attachments": ["att1.pdf|att2.pdf"],
        }
        df = pd.DataFrame(data)

        responses = self.entity_repo.create_or_update_entities_from_dataframe(df, language="en")

        assert len(responses) == 1
        assert responses[0].success is True
        self.created_shared_ids.append(responses[0].shared_id)

    def test_09_roundtrip_dataframe_relationship_property(self):
        """Test the roundtrip issue with relationship properties.

        This test reproduces the issue where:
        1. Entity with relationship property is created
        2. Entity is retrieved as dataframe
        3. The dataframe value (label/sharedId) is used to upload
        4. Verifies the roundtrip works correctly
        """
        if not self.test_template_id:
            pytest.skip("Template not created")

        # Get template to check if relationship property exists
        template = self.template_repo.get_by_id(self.test_template_id)
        if not template:
            pytest.skip("Template not found")

        # Find relationship property name
        rel_prop_name = None
        for prop in template.properties:
            if prop.type == PropertyType.RELATIONSHIP:
                rel_prop_name = prop.name
                break

        if not rel_prop_name:
            pytest.skip("No relationship property found in template")

        print(f"Using relationship property name: {rel_prop_name}")

        # Step 1: Create a target entity (the entity that will be referenced)
        target_entity = Entity(
            title="DF Relationship Target",
            template=self.test_template_id,
            language="en",
        )
        target_shared_id = self.entity_repo.upload(target_entity, "en")
        self.created_shared_ids.append(target_shared_id)
        print(f"Target entity created with shared_id: {target_shared_id}")
        time.sleep(2)

        # Step 2: Create source entity with relationship pointing to target
        # The metadata value should be the target's sharedId
        data = {
            "title": ["DF Relationship Source"],
            "template": [self.test_template_id],
            rel_prop_name: [target_shared_id],  # Use sharedId directly
        }
        df = pd.DataFrame(data)
        print(f"DataFrame for relationship test: {df}")

        responses = self.entity_repo.create_or_update_entities_from_dataframe(df, language="en")
        print(f"Upload responses: {responses}")

        assert len(responses) == 1
        assert responses[0].success is True, f"Failed to upload entity with relationship: {responses[0].error}"
        source_shared_id = responses[0].shared_id
        self.created_shared_ids.append(source_shared_id)

        time.sleep(2)

        # Step 3: Get the entity and verify relationship
        source_entity = self.entity_repo.get_one(source_shared_id, "en")
        assert source_entity is not None
        print(f"Source entity title: {source_entity.title}")
        print(f"Source entity metadata: {source_entity.metadata}")

        # Step 4: Export to DataFrame (roundtrip)
        df_export = self.client.exports.to_dataframe(
            template_name=self.test_template_name,
            language="en",
        )

        assert not df_export.empty
        print(f"Exported DataFrame columns: {df_export.columns.tolist()}")

        # Get the row for our source entity
        source_row = df_export[df_export["sharedId"] == source_shared_id].copy()
        assert len(source_row) > 0, "Source entity not found in exported DataFrame"
        print(f"Source row for roundtrip: {source_row}")
        print(
            f"Relationship value in DataFrame: {source_row[rel_prop_name].values[0] if rel_prop_name in source_row.columns else 'N/A'}"
        )

        # Step 5: Use the DataFrame values to update the entity (roundtrip)
        # The DataFrame value should be the sharedId or label
        roundtrip_responses = self.entity_repo.create_or_update_entities_from_dataframe(source_row, language="en")

        assert len(roundtrip_responses) == 1
        assert roundtrip_responses[0].success is True, f"Roundtrip failed: {roundtrip_responses[0].error}"
        print(f"Roundtrip successful!")

    def test_10_dataframe_roundtrip_consistency(self):
        """Test that getting entities as dataframe and uploading back works correctly."""
        if len(self.created_shared_ids) < 2:
            pytest.skip("Need at least 2 entities for this test")

        # Use template_name to filter entities
        df = self.client.exports.to_dataframe(
            template_name=self.test_template_name,
            language="en",
        )

        assert not df.empty
        print(f"DataFrame columns: {df.columns.tolist()}")
        print(f"DataFrame shape: {df.shape}")

        # Keep only the necessary columns for re-upload
        required_cols = ["sharedId", "title", "template"]
        cols_to_keep = [col for col in required_cols if col in df.columns]
        df = df[cols_to_keep].copy()

        responses = self.entity_repo.create_or_update_entities_from_dataframe(df, language="en")

        for resp in responses:
            assert resp.success is True

    def test_11_upload_with_na_values(self):
        """Test uploading entities with NA/None values in metadata."""
        text_prop = self.property_name_map.get("test_text", "test_text")
        numeric_prop = self.property_name_map.get("test_numeric", "test_numeric")

        data = {
            "title": ["DF NA Test Entity"],
            "template": [self.test_template_id],
            text_prop: [None],
            numeric_prop: [None],
        }
        df = pd.DataFrame(data)

        responses = self.entity_repo.create_or_update_entities_from_dataframe(df, language="en")

        assert len(responses) == 1
        assert responses[0].success is True
        self.created_shared_ids.append(responses[0].shared_id)

    def test_12_error_handling_invalid_template(self):
        """Test error handling when template column has invalid value."""
        data = {
            "title": ["DF Invalid Template Entity"],
            "template": ["nonexistent_template_id_12345"],
        }
        df = pd.DataFrame(data)

        responses = self.entity_repo.create_or_update_entities_from_dataframe(df, language="en")

        assert len(responses) == 1
        assert responses[0].success is False
        assert responses[0].error is not None

    def test_13_mixed_success_and_failure(self):
        """Test DataFrame with mix of valid and invalid entities."""
        data = {
            "title": ["DF Valid Entity", "DF Another Invalid"],
            "template": [self.test_template_id, "invalid_template_id"],
        }
        df = pd.DataFrame(data)

        responses = self.entity_repo.create_or_update_entities_from_dataframe(df, language="en")

        assert len(responses) == 2
        valid_responses = [r for r in responses if r.success]
        invalid_responses = [r for r in responses if not r.success]

        assert len(valid_responses) >= 1
        assert len(invalid_responses) >= 1

        for resp in valid_responses:
            self.created_shared_ids.append(resp.shared_id)

    @classmethod
    def teardown_class(cls):
        """Clean up created entities and template."""
        if cls.created_shared_ids:
            for shared_id in cls.created_shared_ids:
                try:
                    cls.entity_repo.delete(shared_id)
                except (EntityNotFoundError, Exception):
                    pass

        if cls.test_template_id:
            try:
                cls.template_repo.delete_empty_template(cls.test_template_id)
            except Exception:
                pass


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v", "-s"])
