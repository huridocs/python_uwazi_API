import json
from typing import List

from uwazi_api.domain.settings import Settings
from uwazi_api.drivers.http_client import HttpClient


class SettingsRepository:
    def __init__(self, http_client: HttpClient):
        self.http = http_client

    def get(self) -> Settings:
        response = self.http.request_adapter.get(
            url=f"{self.http.url}/api/settings",
            headers=self.http.headers,
            cookies={"connect.sid": self.http.connect_sid},
        )
        data = json.loads(response.text)
        return Settings.model_validate(data)

    def get_languages(self) -> List[str]:
        settings = self.get()
        return [lang.key for lang in settings.languages]
