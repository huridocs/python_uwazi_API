import os
import time
from datetime import datetime
from pathlib import Path

import pytest
from dotenv import load_dotenv

from uwazi_api.client import UwaziClient
from uwazi_api.domain.FileType import FileType
from uwazi_api.domain.entity import Entity


load_dotenv()

UWAZI_USER = os.getenv("UWAZI_USER", "admin")
UWAZI_PASSWORD = os.getenv("UWAZI_PASSWORD", "admin")
UWAZI_URL = os.getenv("UWAZI_URL", "http://localhost:3000")

TEST_DOCUMENT_PATH = Path(__file__).parent.parent.parent / "data" / "test_document.pdf"


class TestFileRepositoryE2E:
    """End-to-end tests for FileRepository using real Uwazi connection."""

    @classmethod
    def setup_class(cls):
        """Set up the client and create test entity for all tests."""
        cls.client = UwaziClient(user=UWAZI_USER, password=UWAZI_PASSWORD, url=UWAZI_URL)
        cls.entity_repo = cls.client.entities
        cls.file_repo = cls.client._file_repo
        cls.file_service = cls.client.files
        cls.template_repo = cls.client.templates

        templates = cls.template_repo.get()
        assert len(templates) > 0, "No templates found in Uwazi instance"
        cls.test_template = templates[0]
        cls.test_template_id = cls.test_template.id

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        cls.test_entity_title = f"test_file_entity_{timestamp}"

        cls.test_entity = Entity(
            title=cls.test_entity_title,
            template=cls.test_template_id,
            language="en",
            published=False,
        )
        cls.test_shared_id = cls.entity_repo.upload(cls.test_entity, "en")
        assert cls.test_shared_id is not None
        time.sleep(2)

        cls.uploaded_document_id: str | None = None
        cls.uploaded_attachment_id: str | None = None
        cls.uploaded_image_data: dict | None = None

    def test_01_upload_file_as_attachment(self):
        """Test upload_file() method to upload a PDF as attachment."""
        result = self.file_repo.upload_file(
            str(TEST_DOCUMENT_PATH),
            self.test_shared_id,
            "en",
            "test_attachment.pdf",
        )
        assert result is True
        time.sleep(2)

        entity = self.entity_repo.get_one(self.test_shared_id, "en")
        assert len(entity.attachments) > 0
        self.__class__.uploaded_attachment_id = entity.attachments[0].id

    def test_02_get_attachment_by_file_name(self):
        """Test get_document_by_file_name() method for attachments."""
        assert self.uploaded_attachment_id is not None
        entity = self.entity_repo.get_one(self.test_shared_id, "en")
        attachment = next((a for a in entity.attachments if a.id == self.uploaded_attachment_id), None)
        assert attachment is not None
        # Use the attachment's filename to retrieve the document
        content = self.file_repo.get_document_by_file_name(attachment.filename)
        assert content is not None
        assert isinstance(content, bytes)
        assert len(content) > 0

    def test_03_get_document_by_nonexistent_file_name(self):
        """Test get_document_by_file_name() with nonexistent file returns None."""
        content = self.file_repo.get_document_by_file_name("nonexistent_file_12345.pdf")
        assert content is None

    def test_04_upload_file_from_bytes_as_attachment(self):
        """Test upload_file_from_bytes() method to upload as attachment."""
        with open(TEST_DOCUMENT_PATH, "rb") as f:
            file_bytes = f.read()

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        result = self.file_repo.upload_file_from_bytes(
            file_bytes,
            self.test_shared_id,
            "en",
            f"test_attachment_2_{timestamp}.pdf",
            "application/pdf",
        )
        assert result is True
        time.sleep(2)

        entity = self.entity_repo.get_one(self.test_shared_id, "en")
        assert len(entity.attachments) > 1

    def test_05_upload_document_from_bytes(self):
        """Test upload_document_from_bytes() method to upload as document."""
        with open(TEST_DOCUMENT_PATH, "rb") as f:
            file_bytes = f.read()

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        doc_title = f"test_doc_from_bytes_{timestamp}.pdf"

        result = self.file_repo.upload_document_from_bytes(
            file_bytes,
            self.test_shared_id,
            "en",
            doc_title,
            FileType.PDF,
        )
        assert result is True
        time.sleep(2)

        entity = self.entity_repo.get_one(self.test_shared_id, "en")
        assert len(entity.documents) > 0
        self.__class__.uploaded_document_id = entity.documents[0].id

    def test_06_upload_image(self):
        """Test upload_image() method with PNG image."""
        png_content = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        image_title = f"test_image_{timestamp}.png"

        result = self.file_repo.upload_image(
            png_content,
            image_title,
            self.test_shared_id,
            "en",
        )
        assert result is not None
        assert "filename" in result or "originalFilename" in result
        self.__class__.uploaded_image_data = result
        time.sleep(2)

    def test_07_get_document_via_file_service(self):
        """Test get_document() method in FileService."""
        content = self.file_service.get_document(self.test_shared_id, "en")
        assert content is not None
        assert isinstance(content, bytes)
        assert len(content) > 0

    def test_08_delete_file_document(self):
        """Test delete_file() method to delete a document."""
        entity = self.entity_repo.get_one(self.test_shared_id, "en")
        assert len(entity.documents) > 0
        doc = entity.documents[0]
        assert doc.id is not None
        doc_id = doc.id

        result = self.file_repo.delete_file(doc_id)
        assert result is True
        time.sleep(2)

        entity = self.entity_repo.get_one(self.test_shared_id, "en")
        remaining_docs = [d for d in entity.documents if d.id == doc_id]
        assert len(remaining_docs) == 0

    def test_09_delete_file_attachment(self):
        """Test delete_file() method to delete an attachment."""
        entity = self.entity_repo.get_one(self.test_shared_id, "en")
        assert len(entity.attachments) > 0
        attachment = entity.attachments[0]
        assert attachment.id is not None
        attachment_id = attachment.id

        result = self.file_repo.delete_file(attachment_id)
        assert result is True
        time.sleep(2)

        entity = self.entity_repo.get_one(self.test_shared_id, "en")
        remaining = [a for a in entity.attachments if a.id == attachment_id]
        assert len(remaining) == 0

    def test_10_delete_nonexistent_file(self):
        """Test delete_file() with nonexistent file_id returns True."""
        result = self.file_repo.delete_file("nonexistent_file_id_12345")
        assert result is True

    @classmethod
    def teardown_class(cls):
        """Clean up test entity."""
        if cls.test_shared_id:
            try:
                cls.entity_repo.delete(cls.test_shared_id)
            except Exception:
                pass


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v", "-s"])
