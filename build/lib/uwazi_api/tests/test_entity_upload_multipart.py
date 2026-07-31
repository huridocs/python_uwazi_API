import json

from uwazi_api.domain.entity_file_upload import EntityFileUpload
from uwazi_api.domain.file_fieldname import FileFieldname
from uwazi_api.domain.FileType import FileType
from uwazi_api.use_cases.repositories.entity_repository import (
    _build_entity_multipart,
)


class TestBuildEntityMultipart:
    """Unit tests for _build_entity_multipart — the multipart body builder.

    These tests verify the ``(files, originalname_fields)`` structure that gets
    passed to ``requests.post(files=..., data=...)``, without any HTTP calls
    or mocks.
    """

    def test_only_entity_no_files(self):
        """When an empty files list is passed, only the entity field is present."""
        payload = {"title": "test", "template": "template_id"}
        request_files, originalname_fields = _build_entity_multipart(payload, [])

        assert len(request_files) == 1
        fieldname, file_info = request_files[0]
        assert fieldname == "entity"
        assert file_info[0] == ""  # empty filename for entity field
        assert json.loads(file_info[1]) == payload
        assert file_info[2] == "application/json"
        assert originalname_fields == {}

    def test_requires_files_list(self):
        """_build_entity_multipart requires a files list — it is no longer optional."""
        import inspect

        sig = inspect.signature(_build_entity_multipart)
        files_param = sig.parameters.get("files")
        assert files_param is not None
        assert files_param.default is inspect.Parameter.empty

    def test_with_single_document_file(self):
        """A single document file is included alongside the entity."""
        payload = {"title": "test"}
        files = [
            EntityFileUpload(
                fieldname=FileFieldname.DOCUMENT,
                filename="report.pdf",
                content=b"%PDF-1.4 binary content",
                content_type=FileType.PDF,
            ),
        ]
        request_files, originalname_fields = _build_entity_multipart(payload, files)

        assert len(request_files) == 2
        # First is always the entity JSON
        assert request_files[0][0] == "entity"
        # Second is the file
        assert request_files[1][0] == "document[0]"
        _, (filename, content, content_type) = request_files[1]
        assert filename == "report.pdf"
        assert content == b"%PDF-1.4 binary content"
        assert content_type == "application/pdf"
        assert originalname_fields == {"document_originalname[0]": "report.pdf"}

    def test_multiple_files_of_different_types(self):
        """Multiple files with different fieldnames are all included."""
        payload = {"title": "multi-file test"}
        files = [
            EntityFileUpload(
                fieldname=FileFieldname.DOCUMENT,
                filename="doc.pdf",
                content=b"%PDF-1.4",
                content_type=FileType.PDF,
            ),
            EntityFileUpload(
                fieldname=FileFieldname.ATTACHMENT,
                filename="image.png",
                content=b"\x89PNG\r\n\x1a\n",
                content_type=FileType.PNG,
            ),
        ]
        request_files, originalname_fields = _build_entity_multipart(payload, files)

        assert len(request_files) == 3  # entity + 2 files
        assert request_files[0][0] == "entity"
        assert request_files[1][0] == "document[0]"
        assert request_files[2][0] == "attachment[1]"
        # Each file carries its own content
        assert request_files[1][1][1] == b"%PDF-1.4"
        assert request_files[2][1][1] == b"\x89PNG\r\n\x1a\n"
        assert originalname_fields == {
            "document_originalname[0]": "doc.pdf",
            "attachment_originalname[1]": "image.png",
        }

    def test_default_fieldname_is_file(self):
        """When fieldname is omitted, it defaults to 'file'."""
        payload = {"title": "test"}
        files = [
            EntityFileUpload(
                filename="data.txt",
                content=b"hello",
            ),
        ]
        request_files, _ = _build_entity_multipart(payload, files)
        assert request_files[1][0] == "file[0]"

    def test_default_content_type(self):
        """When content_type is omitted, defaults to application/octet-stream."""
        payload = {"title": "test"}
        files = [
            EntityFileUpload(
                content=b"binary data",
            ),
        ]
        request_files, _ = _build_entity_multipart(payload, files)
        _, (_, _, content_type) = request_files[1]
        assert content_type == "application/octet-stream"

    def test_default_filename_is_empty(self):
        """When filename is omitted, defaults to empty string."""
        payload = {"title": "test"}
        files = [
            EntityFileUpload(
                content=b"data",
            ),
        ]
        request_files, originalname_fields = _build_entity_multipart(payload, files)
        _, (filename, _, _) = request_files[1]
        assert filename == ""
        assert originalname_fields == {"file_originalname[0]": ""}

    def test_build_multipart_is_always_multipart_structure(self):
        """_build_entity_multipart always returns a multipart-form structure.

        The conditional logic at the repository level (upload()) decides
        whether to send this as multipart/form-data or as a plain JSON POST.
        """
        result_no_files, _ = _build_entity_multipart({"title": "t"}, [])
        assert len(result_no_files) >= 1
        assert result_no_files[0][0] == "entity"

        result_with_files, _ = _build_entity_multipart({"title": "t"}, [EntityFileUpload(content=b"x")])
        assert len(result_with_files) == 2
        assert result_with_files[0][0] == "entity"
        assert result_with_files[1][0] == "file[0]"

    def test_entity_json_serialization(self):
        """The entity payload is correctly JSON-serialized."""
        payload = {
            "title": "Test Entity",
            "template": "abc123",
            "metadata": {"field1": [{"value": "val1"}]},
        }
        request_files, _ = _build_entity_multipart(payload, [])
        _, (_, serialized, _) = request_files[0]
        deserialized = json.loads(serialized)
        assert deserialized == payload

    def test_originalname_fields_preserve_slashes(self):
        """Filenames containing '/' are preserved in the originalname body fields.

        Regression test: Uwazi's parser basenames the Content-Disposition
        filename (``Report No. 111/17.pdf`` → ``17.pdf``), so the full name
        must travel in the ``{field}_originalname[{index}]`` body field.
        """
        payload = {"title": "Report No. 111/17"}
        files = [
            EntityFileUpload(
                fieldname=FileFieldname.FILE,
                filename="Report No. 111/17.pdf",
                content=b"%PDF-1.4",
                content_type=FileType.PDF,
            ),
            EntityFileUpload(
                fieldname=FileFieldname.ATTACHMENT,
                filename="Report No. 111/17.html",
                content=b"<html>",
                content_type=FileType.HTML,
            ),
        ]
        request_files, originalname_fields = _build_entity_multipart(payload, files)

        assert request_files[1][0] == "file[0]"
        assert request_files[2][0] == "attachment[1]"
        assert originalname_fields == {
            "file_originalname[0]": "Report No. 111/17.pdf",
            "attachment_originalname[1]": "Report No. 111/17.html",
        }
