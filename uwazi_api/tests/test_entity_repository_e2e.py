import os
import time
from datetime import datetime
from typing import Optional
import pytest
from dotenv import load_dotenv

from uwazi_api.client import UwaziClient
from uwazi_api.domain.entity import Entity
from uwazi_api.domain.exceptions import EntityNotFoundError, UploadError


load_dotenv()

UWAZI_USER = os.getenv("UWAZI_USER", "admin")
UWAZI_PASSWORD = os.getenv("UWAZI_PASSWORD", "admin")
UWAZI_URL = os.getenv("UWAZI_URL", "http://localhost:3000")


class TestEntityRepositoryE2E:
    """End-to-end tests for EntityRepository using real Uwazi connection."""

    @classmethod
    def setup_class(cls):
        """Set up the client and create test entities for all tests."""
        cls.client = UwaziClient(user=UWAZI_USER, password=UWAZI_PASSWORD, url=UWAZI_URL)
        cls.entity_repo = cls.client.entities
        cls.template_repo = cls.client.templates

        # Get an existing template to use for entity creation
        templates = cls.template_repo.get()
        assert len(templates) > 0, "No templates found in Uwazi instance"
        cls.test_template = templates[0]
        cls.test_template_id = cls.test_template.id

        # Create unique test entity names
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        cls.test_entity_title = f"test_entity_{timestamp}"
        cls.bulk_entity_1_title = f"bulk_test_1_{timestamp}"
        cls.bulk_entity_2_title = f"bulk_test_2_{timestamp}"

        # Create main test entity
        cls.test_entity = Entity(
            title=cls.test_entity_title,
            template=cls.test_template_id,
            language="en",
            published=False,
        )
        cls.test_shared_id: Optional[str] = None
        cls.test_entity_id: Optional[str] = None
        cls.bulk_entity_1_shared_id: Optional[str] = None
        cls.bulk_entity_2_shared_id: Optional[str] = None

        # Upload main test entity
        cls.test_shared_id = cls.entity_repo.upload(cls.test_entity, "en")
        assert cls.test_shared_id is not None
        time.sleep(2)

        # Get entity details
        entity_obj = cls.entity_repo.get_one(cls.test_shared_id, "en")
        cls.test_entity_id = entity_obj.id
        assert cls.test_entity_id is not None

    def test_01_get_one_entity(self):
        """Test get_one() method with valid shared_id."""
        entity = self.entity_repo.get_one(self.test_shared_id, "en")
        assert isinstance(entity, Entity)
        assert entity.shared_id == self.test_shared_id
        assert entity.title == self.test_entity_title
        assert entity.template == self.test_template_id
        assert entity.language == "en"

    def test_02_get_entity_id(self):
        """Test get_id() method returns correct _id."""
        entity_id = self.entity_repo.get_id(self.test_shared_id, "en")
        assert entity_id == self.test_entity_id

    def test_03_get_by_id(self):
        """Test get_by_id() method with valid entity _id."""
        entity = self.entity_repo.get_by_id(self.test_entity_id)
        assert entity is not None
        assert entity.id == self.test_entity_id
        assert entity.shared_id == self.test_shared_id
        assert entity.title == self.test_entity_title

    def test_04_get_by_nonexistent_id(self):
        """Test get_by_id() with nonexistent entity _id."""
        result = self.entity_repo.get_by_id("nonexistent_entity_id_12345")
        assert result is None

    def test_05_get_one_nonexistent_entity(self):
        """Test get_one() with nonexistent shared_id raises EntityNotFoundError."""
        with pytest.raises(EntityNotFoundError):
            self.entity_repo.get_one("nonexistent_shared_id_12345", "en")

    def test_06_update_partially(self):
        """Test update_partially() method to modify entity fields."""
        updated_title = f"{self.test_entity_title}_updated"
        updated_entity = Entity(
            sharedId=self.test_shared_id,
            title=updated_title,
            template=self.test_template_id,
            language="en",
        )
        returned_shared_id = self.entity_repo.update_partially(updated_entity, "en")
        assert returned_shared_id == self.test_shared_id

        # Verify the update
        updated = self.entity_repo.get_one(self.test_shared_id, "en")
        assert updated.title == updated_title

    def test_07_publish_entities(self):
        """Test publish_entities() method to publish entities."""
        # Publish the test entity
        self.entity_repo.publish_entities([self.test_shared_id])

        # Verify published status
        entity = self.entity_repo.get_one(self.test_shared_id, "en")
        assert entity.published is True

    def test_08_create_bulk_entities_for_bulk_ops(self):
        """Create multiple entities for bulk delete testing."""
        # Create first bulk test entity
        bulk_entity_1 = Entity(
            title=self.bulk_entity_1_title,
            template=self.test_template_id,
            language="en",
        )
        self.__class__.bulk_entity_1_shared_id = self.entity_repo.upload(bulk_entity_1, "en")
        assert self.bulk_entity_1_shared_id is not None

        # Create second bulk test entity
        bulk_entity_2 = Entity(
            title=self.bulk_entity_2_title,
            template=self.test_template_id,
            language="en",
        )
        self.__class__.bulk_entity_2_shared_id = self.entity_repo.upload(bulk_entity_2, "en")
        assert self.bulk_entity_2_shared_id is not None
        time.sleep(2)

    def test_09_delete_entities_bulk(self):
        """Test delete_entities() method for bulk deletion."""
        shared_ids = [self.bulk_entity_1_shared_id, self.bulk_entity_2_shared_id]
        self.entity_repo.delete_entities(shared_ids)

        # Verify entities are deleted
        with pytest.raises(EntityNotFoundError):
            self.entity_repo.get_one(self.bulk_entity_1_shared_id, "en")
        with pytest.raises(EntityNotFoundError):
            self.entity_repo.get_one(self.bulk_entity_2_shared_id, "en")

    def test_10_delete_single_entity(self):
        """Test delete() method to remove a single entity."""
        # Delete the main test entity
        self.entity_repo.delete(self.test_shared_id)

        # Verify entity is deleted
        with pytest.raises(EntityNotFoundError):
            self.entity_repo.get_one(self.test_shared_id, "en")

        # Verify get_by_id returns None
        deleted = self.entity_repo.get_by_id(self.test_entity_id)
        assert deleted is None

    def test_11_publish_nonexistent_entity(self):
        """Test publish_entities() with nonexistent shared_id (should not raise)."""
        # Uwazi may not raise error for nonexistent ids in bulk publish
        try:
            self.entity_repo.publish_entities(["nonexistent_shared_id_12345"])
        except UploadError:
            pytest.fail("publish_entities raised UploadError for nonexistent id")

    def test_12_delete_nonexistent_entity(self):
        """Test delete() with nonexistent shared_id (should not raise)."""
        try:
            self.entity_repo.delete("nonexistent_shared_id_12345")
        except UploadError:
            pytest.fail("delete raised UploadError for nonexistent id")

    @classmethod
    def teardown_class(cls):
        """Clean up any remaining test entities."""
        # Delete main test entity if it still exists
        if cls.test_shared_id:
            try:
                cls.entity_repo.delete(cls.test_shared_id)
            except (EntityNotFoundError, UploadError):
                pass

        # Delete bulk entities if they still exist
        if cls.bulk_entity_1_shared_id:
            try:
                cls.entity_repo.delete(cls.bulk_entity_1_shared_id)
            except (EntityNotFoundError, UploadError):
                pass

        if cls.bulk_entity_2_shared_id:
            try:
                cls.entity_repo.delete(cls.bulk_entity_2_shared_id)
            except (EntityNotFoundError, UploadError):
                pass


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v", "-s"])
