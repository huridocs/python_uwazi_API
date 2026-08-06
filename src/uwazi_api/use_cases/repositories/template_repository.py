import json
from typing import Optional

from uwazi_api.domain.exceptions import SearchError
from uwazi_api.domain.template import Template
from uwazi_api.domain.property_schema import PropertySchema
from uwazi_api.adapters.http_client_adapter import HttpClientAdapter
from uwazi_api.domain.sanitize_property_label import PropertyLabelSanitizer


class TemplateRepository:
    def __init__(self, http_client: HttpClientAdapter):
        self.http = http_client
        self._cache = None

    def get(self) -> list[Template]:
        if self._cache is not None:
            return self._cache
        response = self.http.request_adapter.get(
            url=f"{self.http.url}/api/templates",
            headers=self.http.headers,
            cookies={},
        )
        data = json.loads(response.content)
        self._cache = [Template.model_validate(t) for t in data.get("rows", [])]
        return self._cache

    def clear_cache(self) -> None:
        self._cache = None

    def set(self, language: str, template: Template) -> dict:
        self.clear_cache()
        response = self.http.request_adapter.post(
            url=f"{self.http.url}/api/templates",
            headers=self.http.headers,
            cookies={"locale": language},
            data=json.dumps(template.model_dump(by_alias=True, exclude_none=True)),
        )
        return json.loads(response.content)

    def delete_empty_template(self, template_id: str) -> dict:
        self.clear_cache()
        response = self.http.request_adapter.delete(
            url=f"{self.http.url}/api/templates",
            params={"_id": template_id},
            headers=self.http.headers,
            cookies={},
        )
        return json.loads(response.content)

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

    def resolve_template_id(self, template_name_or_id: str) -> Optional[str]:
        template = self.get_by_id(template_name_or_id)
        if template:
            return template.id
        template = self.get_by_name(template_name_or_id)
        if template:
            return template.id
        return None

    def find_property(self, template_name_or_id: str, prop_name: str) -> PropertySchema:
        template_id = self.resolve_template_id(template_name_or_id)
        if not template_id:
            return None
        template = self.get_by_id(template_id)
        all_props = template.properties + template.common_properties
        prop = next((p for p in all_props if p.name == prop_name), None)
        if prop is None:
            normalized = PropertyLabelSanitizer.sanitize(prop_name)
            prop = next((p for p in all_props if PropertyLabelSanitizer.sanitize(p.name) == normalized), None)
        return prop

    def ensure_property_filterable(self, prop, prop_name: str) -> None:
        if not prop.filter:
            raise SearchError(f"Property '{prop_name}' is not filterable")
