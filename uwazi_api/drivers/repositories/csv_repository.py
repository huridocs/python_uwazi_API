import json
from io import BytesIO
from typing import Optional

from uwazi_api.domain import CSVRepositoryInterface, UploadError
from uwazi_api.drivers.http_client import HttpClient


class CSVRepository(CSVRepositoryInterface):
    def __init__(self, http_client: HttpClient):
        self.http = http_client

    def upload(self, template_id: str, csv_bytes: bytes, filename: str = "import.csv") -> Optional[dict]:
        response = self.http.request_adapter.post(
            url=f"{self.http.url}/api/import",
            data={"template": template_id},
            files={"file": (filename, BytesIO(csv_bytes), "application/csv")},
            cookies={"connect.sid": self.http.connect_sid},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        if response.status_code != 200:
            self.http.graylog.info(f"Error uploading CSV {response.status_code}")
            raise UploadError(f"Error uploading CSV {response.status_code} {response.text}")
        self.http.graylog.info(f"CSV uploaded with status {response.status_code}")
        return {"status_code": response.status_code, "text": response.text}
