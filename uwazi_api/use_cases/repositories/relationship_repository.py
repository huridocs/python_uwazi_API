import json
from typing import Optional

from uwazi_api.domain.reference import Reference
from uwazi_api.domain.relationship_type import RelationshipType
from uwazi_api.domain.exceptions import UploadError
from uwazi_api.adapters.http_client_adapter import HttpClientAdapter


class RelationshipRepository:
    def __init__(self, http_client: HttpClientAdapter):
        self.http = http_client
        self._cache = None

    def get_relation_types(self) -> list[RelationshipType]:
        if self._cache is not None:
            return self._cache
        response = self.http.request_adapter.get(
            url=f"{self.http.url}/api/relationtypes",
            headers=self.http.headers,
            cookies={"connect.sid": self.http.connect_sid},
        )
        data = json.loads(response.text)
        if isinstance(data, list):
            self._cache = [RelationshipType.model_validate(rt) for rt in data]
        else:
            self._cache = [RelationshipType.model_validate(rt) for rt in data.get("rows", [])]
        return self._cache

    def clear_cache(self) -> None:
        self._cache = None

    def create_relation_type(self, name: str, language: str = "en") -> dict:
        self.clear_cache()
        json_data = {"name": name}
        response = self.http.request_adapter.post(
            url=f"{self.http.url}/api/relationtypes",
            headers=self.http.headers,
            cookies={"connect.sid": self.http.connect_sid, "locale": language},
            json=json_data,
        )
        return json.loads(response.text)

    def get_relation_type_by_name(self, name: str) -> Optional[RelationshipType]:
        relation_types = self.get_relation_types()
        for rt in relation_types:
            if rt.name == name:
                return rt
        return None

    def get_relation_type_by_id(self, relation_type_id: str) -> Optional[RelationshipType]:
        relation_types = self.get_relation_types()
        for rt in relation_types:
            if rt.id == relation_type_id:
                return rt
        return None

    def resolve_relation_type_id(self, relation_type_name_or_id: str) -> Optional[str]:
        relation_type = self.get_relation_type_by_id(relation_type_name_or_id)
        if relation_type:
            return relation_type.id
        relation_type = self.get_relation_type_by_name(relation_type_name_or_id)
        if relation_type:
            return relation_type.id
        return None

    def create(
        self,
        file_entity_shared_id: str,
        file_id: str,
        reference: Reference,
        to_entity_shared_id: str,
        relationship_type_id: str,
        language: str = "en",
    ) -> Optional[dict]:
        relationship_from = {
            "entity": file_entity_shared_id,
            "file": file_id,
            "template": None,
            "reference": reference.model_dump(by_alias=True),
        }
        relationship_to = {
            "entity": to_entity_shared_id,
            "template": relationship_type_id,
        }
        json_data = {"delete": [], "save": [[relationship_from, relationship_to]]}

        response = self.http.request_adapter.post(
            url=f"{self.http.url}/api/relationships/bulk",
            headers=self.http.headers,
            cookies={"connect.sid": self.http.connect_sid, "locale": language},
            data=json.dumps(json_data),
        )
        if response.status_code != 200:
            message = f"Error setting relationships {response.status_code} {response.text}"
            self.http.graylog.error(message)
            raise UploadError(message)
        self.http.graylog.info("Relationships set successfully")
        return json.loads(response.text)
