import json
from typing import List

from uwazi_api.domain.thesauri import Thesauri
from uwazi_api.drivers.http_client import HttpClient


class ThesauriRepository:
    def __init__(self, http_client: HttpClient):
        self.http = http_client
        self._cache = {}

    def get(self, language: str) -> List[Thesauri]:
        if language in self._cache:
            return self._cache[language]
        response = self.http.request_adapter.get(
            url=f"{self.http.url}/api/thesauris",
            headers=self.http.headers,
            cookies={"connect.sid": self.http.connect_sid, "locale": language},
        )
        data = json.loads(response.content.decode("utf-8"))
        self._cache[language] = [Thesauri.model_validate(t) for t in data.get("rows", [])]
        return self._cache[language]

    def clear_cache(self, language: str = None) -> None:
        if language is None:
            self._cache.clear()
        else:
            self._cache.pop(language, None)

    def add_value(self, thesauri_name: str, thesauri_id: str, thesauri_values: dict, language: str) -> dict:
        self.clear_cache(language)
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
