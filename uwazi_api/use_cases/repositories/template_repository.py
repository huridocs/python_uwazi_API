import json
from typing import List, Optional

from uwazi_api.domain.template import Template
from uwazi_api.adapters.http_client_adapter import HttpClientAdapter


class TemplateRepository:
    def __init__(self, http_client: HttpClientAdapter):
        self.http = http_client
        self._cache = None

    def get(self) -> List[Template]:
        if self._cache is not None:
            return self._cache
        response = self.http.request_adapter.get(
            url=f"{self.http.url}/api/templates",
            headers=self.http.headers,
            cookies={"connect.sid": self.http.connect_sid},
        )
        data = json.loads(response.text)
        self._cache = [Template.model_validate(t) for t in data.get("rows", [])]
        return self._cache

    def clear_cache(self) -> None:
        self._cache = None

    def set(self, language: str, template: dict) -> dict:
        self.clear_cache()
        response = self.http.request_adapter.post(
            url=f"{self.http.url}/api/templates",
            headers=self.http.headers,
            cookies={"connect.sid": self.http.connect_sid, "locale": language},
            data=json.dumps(template),
        )
        return json.loads(response.text)

    def get_by_name(self, template_name: str) -> Optional[Template]:
        templates = self.get()
        for t in templates:
            if t.name == template_name:
                return t
        return None

    def get_by_id(self, template_id: str) -> Optional[Template]:
        templates = self.get()
        for t in templates:
            if t.id == template_id:
                return t
        return None
