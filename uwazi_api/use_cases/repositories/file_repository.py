import json
from requests.exceptions import RequestException, RetryError

from uwazi_api.domain.FileType import FileType
from uwazi_api.adapters.http_client_adapter import HttpClientAdapter


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
            response = self.http.request_adapter.post(
                url=f"{self.http.url}/api/files/upload/document",
                data={"entity": share_id},
                files={"file": (title, file_bytes, str(file_type))},
                cookies={"locale": language},
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
            data = json.loads(response.text)
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
            response = self.http.request_adapter.post(
                url=f"{self.http.url}/api/files/upload/attachment",
                data={"entity": share_id},
                files={"file": (title, file_bytes, file_type)},
                cookies={"locale": language},
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
            data = json.loads(response.text)
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
            response = self.http.request_adapter.post(
                url=f"{self.http.url}/api/files/upload/attachment",
                data={"entity": entity_shared_id},
                files={"file": (title, image_binary, "image/png")},
                cookies={"locale": language},
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
            data = json.loads(response.text)
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
