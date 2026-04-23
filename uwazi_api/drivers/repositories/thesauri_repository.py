import json
from typing import List

from uwazi_api.domain import Thesauri, ThesauriRepositoryInterface
from uwazi_api.drivers.http_client import HttpClient


class ThesauriRepository(ThesauriRepositoryInterface):
    def __init__(self, http_client: HttpClient):
        self.http = http_client

    def get(self, language: str) -> List[Thesauri]:
        response = self.http.request_adapter.get(
            url=f"{self.http.url}/api/thesauris",
            headers=self.http.headers,
            cookies={"connect.sid": self.http.connect_sid, "locale": language},
        )
        data = json.loads(response.content.decode("utf-8"))
        return [Thesauri.model_validate(t) for t in data.get("rows", [])]

    def add_value(self, thesauri_name: str, thesauri_id: str, thesauri_values: dict, language: str) -> dict:
        data = {
            "_id": thesauri_id,
            "name": thesauri_name,
            "values": [{"label": x, "id": thesauri_values[x]} for x in thesauri_values],
        }
        response = self.http.request_adapter.post(
            url=f"{self.http.url}/api/thesauris",
            headers=self.http.headers,
            cookies={"connect.sid": self.http.connect_sid, "locale": language},
            data=json.dumps(data),
        )
        return json.loads(response.content)
