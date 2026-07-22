import json
import uuid
from requests.exceptions import RequestException, RetryError

from uwazi_api.domain.FileType import FileType
from uwazi_api.adapters.http_client_adapter import HttpClientAdapter


def _build_multipart_body(entity_id: str, title: str, file_bytes: bytes, content_type: str) -> tuple[bytes, str]:
    """Build a multipart/form-data body for Uwazi's upload endpoints.

    Uwazi reads the ``Content-Disposition`` filename as Latin-1, while the
    ``requests`` library encodes ``str`` filenames as UTF-8. Sending UTF-8
    bytes in the header causes double-encoding
    (``ã`` → ``Ã£`` → ``ÃƒÂ£``).

    To preserve all Unicode characters in the primary document name, we
    send the human-readable name in a separate ``originalname`` text field
    encoded as UTF-8. Uwazi's ``UploadMiddleware.processOriginalFileName``
    prefers that body field over the multipart header, so the stored
    ``originalname`` (the document name users see) is correct for Latin-1
    accents, CJK, emoji, and any other UTF-8 characters.
    """
    boundary = f"----UwaziAPIBoundary{uuid.uuid4().hex}"
    title_utf8 = title.encode("utf-8")

    # Encode the header filename as Latin-1 when possible so the legacy
    # header-based path also decodes correctly; characters outside Latin-1
    # fall back to UTF-8, but those are overridden by the originalname field.
    try:
        header_filename_bytes = title.encode("latin-1")
    except UnicodeEncodeError:
        header_filename_bytes = title_utf8

    body = (
        f"--{boundary}\r\n".encode()
        + b'Content-Disposition: form-data; name="entity"\r\n\r\n'
        + f"{entity_id}\r\n".encode()
        + f"--{boundary}\r\n".encode()
        + b'Content-Disposition: form-data; name="originalname"\r\n'
        + b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        + title_utf8
        + f"\r\n--{boundary}\r\n".encode()
        + b'Content-Disposition: form-data; name="file"; filename="'
        + header_filename_bytes
        + b'"\r\n'
        + f"Content-Type: {content_type}\r\n\r\n".encode()
        + file_bytes
        + f"\r\n--{boundary}--\r\n".encode()
    )
    return body, boundary


class FileRepository:
    def __init__(self, http_client: HttpClientAdapter):
        self.http = http_client

    def get_document_by_file_name(self, file_name: str) -> bytes | None:
        document_response = self.http.request_adapter.get(
            url=f"{self.http.url}/api/files/{file_name}",
            headers=self.http.headers,
            cookies={},
        )
        if document_response.status_code != 200:
            self.http.graylog.info(f"No document found for {file_name}")
            return None
        return document_response.content

    def upload_file(self, pdf_file_path: str, share_id: str, language: str, title: str) -> bool:
        try:
            with open(pdf_file_path, "rb") as pdf_file:
                return self.upload_file_from_bytes(pdf_file.read(), share_id, language, title, "application/pdf")
        except FileNotFoundError:
            self.http.graylog.info(f"No pdf found {pdf_file_path}")
            return False

    def upload_document_from_bytes(
        self, file_bytes: bytes, share_id: str, language: str, title: str, file_type: FileType = FileType.PDF
    ) -> bool:
        try:
            body, boundary = _build_multipart_body(share_id, title, file_bytes, str(file_type))
            response = self.http.request_adapter.post(
                url=f"{self.http.url}/api/files/upload/document",
                data=body,
                cookies={"locale": language},
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                },
            )
            data = json.loads(response.content)
            if "prettyMessage" in data:
                self.http.graylog.error(f"Upload error for {share_id}: {data.get('prettyMessage')}")
                return False
            return True
        except RequestException as e:
            self.http.graylog.error(f"Network error uploading document for {share_id}: {e}")
            return False
        except json.JSONDecodeError as e:
            self.http.graylog.error(f"Invalid JSON response uploading document for {share_id}: {e}")
            return False

    def upload_file_from_bytes(self, file_bytes: bytes, share_id: str, language: str, title: str, file_type: str) -> bool:
        try:
            body, boundary = _build_multipart_body(share_id, title, file_bytes, file_type)
            response = self.http.request_adapter.post(
                url=f"{self.http.url}/api/files/upload/attachment",
                data=body,
                cookies={"locale": language},
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                },
            )
            data = json.loads(response.content)
            if "prettyMessage" in data:
                self.http.graylog.error(f"Upload error for {share_id}: {data.get('prettyMessage')}")
                return False
            return True
        except RequestException as e:
            self.http.graylog.error(f"Network error uploading file for {share_id}: {e}")
            return False
        except json.JSONDecodeError as e:
            self.http.graylog.error(f"Invalid JSON response uploading file for {share_id}: {e}")
            return False

    def upload_image(self, image_binary: bytes, title: str, entity_shared_id: str, language: str) -> dict | None:
        try:
            body, boundary = _build_multipart_body(entity_shared_id, title, image_binary, "image/png")
            response = self.http.request_adapter.post(
                url=f"{self.http.url}/api/files/upload/attachment",
                data=body,
                cookies={"locale": language},
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                },
            )
            data = json.loads(response.content)
            if "prettyMessage" in data:
                self.http.graylog.error(f"Upload error for {entity_shared_id}: {data.get('prettyMessage')}")
                return None
            return data
        except RequestException as e:
            self.http.graylog.error(f"Network error uploading image for {entity_shared_id}: {e}")
            return None
        except json.JSONDecodeError as e:
            self.http.graylog.error(f"Invalid JSON response uploading image for {entity_shared_id}: {e}")
            return None

    def delete_file(self, file_id: str) -> bool:
        params = (("_id", file_id),)
        try:
            response = self.http.request_adapter.delete(
                url=f"{self.http.url}/api/files",
                cookies={},
                params=params,
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
            if response.status_code != 200:
                self.http.graylog.error(f"Error deleting file {file_id}: {response.status_code}")
                return False
            return True
        except RetryError as e:
            self.http.graylog.error(f"Retry error deleting file {file_id}: {e}")
            return False
