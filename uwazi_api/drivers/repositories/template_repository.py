import json
from typing import List, Optional

from uwazi_api.domain import Template, TemplateRepositoryInterface, TemplateNotFoundError
from uwazi_api.drivers.http_client import HttpClient


class TemplateRepository(TemplateRepositoryInterface):
    def __init__(self, http_client: HttpClient):
        self.http = http_client

    def get(self) -> List[Template]:
        response = self.http.request_adapter.get(
            url=f"{self.http.url}/api/templates",
            headers=self.http.headers,
            cookies={"connect.sid": self.http.connect_sid},
        )
        data = json.loads(response.text)
        return [Template.model_validate(t) for t in data.get("rows", [])]

    def set(self, language: str, template: dict) -> dict:
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
