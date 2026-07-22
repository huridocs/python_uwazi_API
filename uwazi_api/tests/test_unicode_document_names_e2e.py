"""End-to-end test: entity titles and primary-document filenames with
non-ASCII characters (accents, tildes, etc.) must survive a round-trip
through the Uwazi API without mojibake.

The bug: ``requests`` decodes ``response.text`` using the charset from the
``Content-Type`` header, defaulting to ISO-8859-1 when no charset is present.
Uwazi responses typically have ``Content-Type: application/json`` without an
explicit charset, so ``response.text`` corrupts UTF-8 multi-byte sequences
(e.g. ``ã`` → ``Ã£``).  The fix is to parse JSON from ``response.content``
(raw bytes), which ``json.loads`` always decodes as UTF-8 per RFC 8259.
"""

import os
import time
from datetime import datetime
from pathlib import Path

import pytest
from dotenv import load_dotenv

from uwazi_api.client import UwaziClient
from uwazi_api.domain.entity import Entity
from uwazi_api.domain.FileType import FileType

load_dotenv()

UWAZI_USER = os.getenv("UWAZI_USER", "admin")
UWAZI_PASSWORD = os.getenv("UWAZI_PASSWORD", "admin")
UWAZI_URL = os.getenv("UWAZI_URL", "http://localhost:3000")

TEST_DOCUMENT_PATH = Path(__file__).parent.parent.parent / "data" / "test_document.pdf"

# The real-world title from the bug report — contains "ã" (U+00E3) which
# in UTF-8 is the bytes C3 A3.  When mis-decoded as Latin-1 those two bytes
# become "Ã£", the mojibake seen in the bug.
UNICODE_TITLE = (
    "Caso Cley Mendes y otros (Chacina do Tapanã) Vs. Brasil. "
    "Excepciones Preliminares, Fondo, Reparaciones y Costas. "
    "Sentencia de 25 de noviembre de 2025"
)
# The mojibake we expect to see if the bug is present:
MOJIBAKE_TITLE = UNICODE_TITLE.replace("ã", "Ã£")


class TestUnicodeDocumentNamesE2E:
    """End-to-end tests verifying that non-ASCII characters in titles and
    primary document filenames are preserved through the API."""

    @classmethod
    def setup_class(cls):
        """Create a test entity with a Unicode-heavy title, then upload a
        primary document whose title also contains non-ASCII characters."""
        cls.client = UwaziClient(user=UWAZI_USER, password=UWAZI_PASSWORD, url=UWAZI_URL)
        cls.entity_repo = cls.client.entities
        cls.file_repo = cls.client._file_repo
        cls.file_service = cls.client.files
        cls.template_repo = cls.client.templates

        templates = cls.template_repo.get()
        assert len(templates) > 0, "No templates found in Uwazi instance"
        cls.test_template = templates[0]
        cls.test_template_id = cls.test_template.id

        cls.test_entity = Entity(
            title=UNICODE_TITLE,
            template=cls.test_template_id,
            language="en",
            published=False,
        )
        cls.test_shared_id = cls.entity_repo.upload(cls.test_entity, "en")
        assert cls.test_shared_id is not None
        time.sleep(2)

        # Upload a primary document with a Unicode filename
        with open(TEST_DOCUMENT_PATH, "rb") as f:
            file_bytes = f.read()

        cls.doc_title = "Sentencia de 25 de noviembre de 2025 ñáéíóú.pdf"
        result = cls.file_repo.upload_document_from_bytes(
            file_bytes,
            cls.test_shared_id,
            "en",
            cls.doc_title,
            FileType.PDF,
        )
        assert result is True
        time.sleep(2)

    def test_01_entity_title_preserves_unicode(self):
        """The entity title round-trips without mojibake."""
        entity = self.entity_repo.get_one(self.test_shared_id, "en")
        assert entity.title == UNICODE_TITLE, f"Title mojibake: expected '{UNICODE_TITLE}', got '{entity.title}'"
        # Explicitly check that the bug-specific mojibake is NOT present
        assert "Ã£" not in entity.title, "Mojibake detected in entity title"

    def test_02_document_originalname_preserves_unicode(self):
        """The primary document originalname (the human-readable title we
        uploaded) round-trips without mojibake."""
        entity = self.entity_repo.get_one(self.test_shared_id, "en")
        assert len(entity.documents) > 0, "No documents found on entity"
        doc = entity.documents[0]
        # originalname is what Uwazi stores from the upload title;
        # it must not contain mojibake.
        assert "Ã" not in doc.originalname, f"Mojibake detected in document originalname: {doc.originalname}"
        assert doc.originalname == self.doc_title, (
            f"originalname mismatch: expected '{self.doc_title}', got '{doc.originalname}'"
        )

    def test_03_get_by_id_preserves_unicode(self):
        """get_by_id also preserves Unicode in the title."""
        entity = self.entity_repo.get_one(self.test_shared_id, "en")
        entity_by_id = self.entity_repo.get_by_id(entity.id)
        assert entity_by_id is not None
        assert entity_by_id.title == UNICODE_TITLE
        assert "Ã£" not in entity_by_id.title

    def test_04_search_preserves_unicode(self):
        """Search results also preserve Unicode in entity titles."""
        # Search for the entity by its shared_id via the search repo
        search_repo = self.client.search
        shared_ids = search_repo.get_shared_ids(self.test_template.name, batch_size=100, unpublished=True)
        assert self.test_shared_id in shared_ids, "Test entity not found in search results"

    def test_05_get_document_by_unicode_filename(self):
        """Retrieving the document by its Unicode filename works and returns content."""
        entity = self.entity_repo.get_one(self.test_shared_id, "en")
        assert len(entity.documents) > 0
        filename = entity.documents[0].filename
        content = self.file_repo.get_document_by_file_name(filename)
        assert content is not None
        assert isinstance(content, bytes)
        assert len(content) > 0

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
