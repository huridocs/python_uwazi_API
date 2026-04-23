import json
from datetime import date, datetime
from datetime import timezone
from typing import List, Optional

from uwazi_api.domain.entity import Entity
from uwazi_api.domain.exceptions import (
    EntityNotFoundError,
    SearchError,
    UploadError,
)
from uwazi_api.adapters.http_client_adapter import HttpClientAdapter
from uwazi_api.use_cases.repositories.search_repository import SearchRepository
from uwazi_api.use_cases.repositories.template_repository import TemplateRepository


class EntityRepository(SearchRepository):
    def __init__(
        self,
        http_client: HttpClientAdapter,
        template_repo: Optional[TemplateRepository] = None,
    ):
        super().__init__(http_client, template_repo=template_repo)

    def get_one(self, shared_id: str, language: str) -> Entity:
        response = self.http.request_adapter.get(
            url=f"{self.http.url}/api/entities",
            headers=self.http.headers,
            cookies={"connect.sid": self.http.connect_sid, "locale": language},
            params={"sharedId": shared_id, "omitRelationships": "true"},
        )
        if response.status_code != 200:
            raise EntityNotFoundError(f"Error getting entity {shared_id} {language}: {response.status_code}")
        data = json.loads(response.text)
        rows = data.get("rows", [])
        if len(rows) == 0:
            raise EntityNotFoundError(f"Entity not found {shared_id} {language}")
        return Entity.model_validate(rows[0])

    def get_id(self, shared_id: str, language: str) -> str:
        entity = self.get_one(shared_id, language)
        if not entity.id:
            raise EntityNotFoundError(f"Entity {shared_id} has no _id")
        return entity.id

    def get_by_id(self, entity_id: str) -> Optional[Entity]:
        response = self.http.request_adapter.get(
            url=f"{self.http.url}/api/entities",
            headers=self.http.headers,
            cookies={"connect.sid": self.http.connect_sid},
            params={"_id": entity_id, "omitRelationships": "true"},
        )
        if response.status_code != 200:
            return None
        rows = json.loads(response.text).get("rows", [])
        if len(rows) == 0:
            return None
        return Entity.model_validate(rows[0])

    def upload(self, entity: Entity, language: str) -> str:
        if entity.template and self._template_repo:
            entity.template = self._resolve_template_id(entity.template)
        self._validate_metadata(entity)
        payload = entity.model_dump(by_alias=True, exclude_none=True)
        if "metadata" in payload:
            payload["metadata"] = self._normalize_metadata(payload["metadata"], entity)
        upload_response = self.http.request_adapter.post(
            url=f"{self.http.url}/api/entities",
            headers=self.http.headers,
            cookies={"connect.sid": self.http.connect_sid, "locale": language},
            data=json.dumps(payload),
        )
        if upload_response.status_code != 200:
            message = f"Error uploading entity {upload_response.status_code} {upload_response.text}"
            self.http.graylog.error(message)
            raise UploadError(message)
        if entity.id:
            self.http.graylog.info(f"Entity uploaded {entity.id}")
        data = json.loads(upload_response.text)
        return data["sharedId"]

    def _normalize_metadata(self, metadata: dict, entity: Entity) -> dict:
        normalized = {}
        name_map = self._get_property_name_map(entity.template) if self._template_repo and entity.template else {}
        prop_type_map = self._get_property_type_map(entity.template) if self._template_repo and entity.template else {}
        for key, value in metadata.items():
            normalized_key = name_map.get(key, key)
            prop_type = prop_type_map.get(normalized_key)
            converted = self._convert_value(value, prop_type)
            if isinstance(converted, list):
                normalized[normalized_key] = [{"value": v} if not isinstance(v, dict) else v for v in converted]
            else:
                normalized[normalized_key] = [{"value": converted} if not isinstance(converted, dict) else converted]
        return normalized

    def _convert_value(self, value, prop_type: Optional[str]):
        if isinstance(value, date) and not isinstance(value, datetime):
            return int(datetime.combine(value, datetime.min.time()).replace(tzinfo=timezone.utc).timestamp())
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return int(value.timestamp())
        if prop_type in ("geolocation",) and isinstance(value, str) and "|" in value:
            lat, lon = value.split("|")
            return {"lat": float(lat), "lon": float(lon)}
        if prop_type in ("relationship",) and isinstance(value, str):
            return {"label": value}
        if isinstance(value, list):
            return [self._convert_value(v, prop_type) for v in value]
        return value

    def _get_property_type_map(self, template_name_or_id: str) -> dict:
        template_id = self._resolve_template_id(template_name_or_id)
        template = self._template_repo.get_by_id(template_id) if self._template_repo else None
        if not template:
            return {}
        all_props = template.properties + template.common_properties
        return {p.name: p.type for p in all_props}

    def _get_property_name_map(self, template_name_or_id: str) -> dict:
        template_id = self._resolve_template_id(template_name_or_id)
        template = self._template_repo.get_by_id(template_id) if self._template_repo else None
        if not template:
            return {}
        all_props = template.properties + template.common_properties
        name_map = {}
        for p in all_props:
            normalized = self._template_repo._normalize_name(p.name)
            name_map[p.name] = p.name
            name_map[normalized] = p.name
        return name_map

    @staticmethod
    def _normalize_name(name: str) -> str:
        return "".join(ch if ch.isalnum() else "_" for ch in name.lower())

    def _validate_metadata(self, entity: Entity) -> None:
        if not self._template_repo or not entity.template:
            return
        template = self._template_repo.get_by_id(entity.template)
        if not template:
            raise SearchError(f"Template '{entity.template}' not found")
        all_props = template.properties + template.common_properties
        valid_names = {p.name for p in all_props}
        metadata_keys = set(entity.metadata.keys()) if entity.metadata else set()
        for key in entity.metadata or {}:
            if key not in valid_names:
                raise SearchError(f"Metadata property '{key}' not found in template '{template.name}'")
        for prop in all_props:
            if prop.required and prop.name not in metadata_keys:
                raise SearchError(f"Required property '{prop.name}' is missing in entity metadata")

    def delete(self, shared_id: str) -> None:
        response = self.http.request_adapter.delete(
            f"{self.http.url}/api/documents",
            headers=self.http.headers,
            params={"sharedId": shared_id},
            cookies={"connect.sid": self.http.connect_sid},
        )
        if response.status_code != 200:
            message = f"Error ({response.status_code}) deleting entity {shared_id}"
            self.http.graylog.info(message)
            raise UploadError(message)
        self.http.graylog.info(f"Entity deleted {shared_id}")

    def publish_entities(self, shared_ids: List[str]) -> None:
        payload = {"ids": shared_ids, "values": {"published": True}}
        response = self.http.request_adapter.post(
            url=f"{self.http.url}/api/entities/multipleupdate",
            headers=self.http.headers,
            cookies={"connect.sid": self.http.connect_sid},
            data=json.dumps(payload),
        )
        if response.status_code != 200:
            message = f"Error ({response.status_code}) publishing entities {shared_ids}"
            self.http.graylog.info(message)
            raise UploadError(message)
        self.http.graylog.info(f"Entities published {shared_ids}")

    def delete_entities(self, shared_ids: List[str]) -> None:
        payload = {"sharedIds": shared_ids}
        response = self.http.request_adapter.post(
            url=f"{self.http.url}/api/entities/bulkdelete",
            headers=self.http.headers,
            cookies={"connect.sid": self.http.connect_sid},
            data=json.dumps(payload),
        )
        if response.status_code != 200:
            message = f"Error ({response.status_code}) deleting entities {shared_ids}"
            self.http.graylog.info(message)
            raise UploadError(message)
        self.http.graylog.info(f"Entities deleted {shared_ids}")
