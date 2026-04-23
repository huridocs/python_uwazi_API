import json
import os
from pathlib import Path
from typing import List, Optional

from requests.exceptions import RetryError

from uwazi_api.domain.interfaces import FileRepositoryInterface
from uwazi_api.drivers.http_client import HttpClient


class FileRepository(FileRepositoryInterface):
    language_to_file_language = {"fr": "fra", "es": "spa", "en": "eng", "pt": "prt", "ar": "arb"}

    def __init__(self, http_client: HttpClient):
        self.http = http_client

    def get_document_by_file_name(self, file_name: str) -> Optional[bytes]:
        document_response = self.http.request_adapter.get(
            url=f"{self.http.url}/api/files/{file_name}",
            headers=self.http.headers,
            cookies={"connect.sid": self.http.connect_sid},
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
        self, file_bytes: bytes, share_id: str, language: str, title: str, file_type: str
    ) -> bool:
        try:
            unicode_escape_title = title.encode("utf-8").decode("unicode-escape")
            response = self.http.request_adapter.post(
                url=f"{self.http.url}/api/files/upload/document",
                data={"entity": share_id},
                files={"file": (unicode_escape_title, file_bytes, file_type)},
                cookies={"connect.sid": self.http.connect_sid, "locale": language},
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
            data = json.loads(response.text)
            if "prettyMessage" in data:
                self.http.graylog.info(f"Upload error: {data.get('prettyMessage')}")
                return False
            return True
        except Exception:
            self.http.graylog.info(f"Uploading without response {share_id} {title}")
            return False

    def upload_file_from_bytes(self, file_bytes: bytes, share_id: str, language: str, title: str, file_type: str) -> bool:
        try:
            unicode_escape_title = title.encode("utf-8").decode("unicode-escape")
            response = self.http.request_adapter.post(
                url=f"{self.http.url}/api/files/upload/attachment",
                data={"entity": share_id},
                files={"file": (unicode_escape_title, file_bytes, file_type)},
                cookies={"connect.sid": self.http.connect_sid, "locale": language},
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
            data = json.loads(response.text)
            if "prettyMessage" in data:
                self.http.graylog.info(f"Upload error: {data.get('prettyMessage')}")
                return False
            return True
        except Exception:
            self.http.graylog.info(f"Uploading without response {share_id} {title}")
            return False

    def upload_image(self, image_binary: bytes, title: str, entity_shared_id: str, language: str) -> Optional[dict]:
        try:
            response = self.http.request_adapter.post(
                url=f"{self.http.url}/api/files/upload/attachment",
                data={"entity": entity_shared_id},
                files={"file": (title, image_binary, "image/png")},
                cookies={"connect.sid": self.http.connect_sid, "locale": language},
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
            data = json.loads(response.text)
            if "prettyMessage" in data:
                self.http.graylog.info(f"Upload error: {data.get('prettyMessage')}")
                return None
            return data
        except Exception:
            self.http.graylog.info(f"Uploading without response {entity_shared_id} {language} {title}")
            return None

    def delete_file(self, file_id: str) -> bool:
        params = (("_id", file_id),)
        try:
            self.http.request_adapter.delete(
                url=f"{self.http.url}/api/files",
                cookies={"connect.sid": self.http.connect_sid},
                params=params,
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
        except RetryError:
            return False
        return True
