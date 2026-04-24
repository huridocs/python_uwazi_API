import json
import re
from datetime import date, datetime
from datetime import timezone
from typing import Any, Optional

from uwazi_api.domain.entity import Entity
from uwazi_api.domain.exceptions import (
    EntityNotFoundError,
    SearchError,
    UploadError,
)
from uwazi_api.adapters.http_client_adapter import HttpClientAdapter
from uwazi_api.use_cases.repositories.search_repository import SearchRepository
from uwazi_api.use_cases.repositories.template_repository import TemplateRepository
from uwazi_api.use_cases.repositories.thesauri_repository import ThesauriRepository


class EntityRepository(SearchRepository):
    def __init__(
        self,
        http_client: HttpClientAdapter,
        template_repo: Optional[TemplateRepository] = None,
        thesauri_repo: Optional[ThesauriRepository] = None,
    ):
        super().__init__(http_client, template_repo=template_repo, thesauri_repo=thesauri_repo)

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
        if entity.shared_id and not entity.id:
            existing = self.get_one(entity.shared_id, language)
            entity.id = existing.id

        if entity.template and self._template_repo:
            entity.template = self._resolve_template_id(entity.template)

        # Preprocess metadata to standard format before validation
        prop_type_map = {}
        name_map = {}
        if self._template_repo and entity.template:
            prop_type_map = self._get_property_type_map(entity.template)
            name_map = self._get_property_name_map(entity.template)
        if entity.metadata:
            entity.metadata = self._preprocess_metadata(entity.metadata, prop_type_map, name_map)

        # Resolve thesaurus labels to UUIDs for select/multiselect
        if entity.metadata and self._thesauri_repo and entity.template:
            self._resolve_metadata_labels(entity.metadata, entity.template, language)

        self._validate_metadata(entity)
        payload = entity.model_dump(by_alias=True, exclude_none=True)
        # Metadata is already preprocessed, no need to normalize again
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

    def _preprocess_metadata(self, metadata: dict, prop_type_map: dict, name_map: dict) -> dict:
        preprocessed = {}
        for key, value in metadata.items():
            normalized_key = name_map.get(key, key) if name_map else key
            prop_type = prop_type_map.get(normalized_key) if prop_type_map else None
            converted = self._convert_value(value, prop_type)
            if isinstance(converted, list):
                processed_items = []
                for item in converted:
                    if isinstance(item, dict) and "value" in item:
                        processed_items.append(item)
                    else:
                        processed_items.append({"value": item})
                preprocessed[normalized_key] = processed_items
            else:
                preprocessed[normalized_key] = [{"value": converted}]
        return preprocessed

    def _resolve_metadata_labels(self, metadata: dict, template_id: str, language: str) -> None:
        """Resolve thesaurus labels to UUIDs for select/multiselect properties."""
        if not self._thesauri_repo or not self._template_repo or not template_id:
            return
        template = self._template_repo.get_by_id(template_id)
        if not template:
            return
        all_props = template.properties + template.common_properties
        thesauri_cache = {}  # thesaurus_id -> (label_to_id, valid_ids)
        uuid_pattern = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
        for key, values in metadata.items():
            prop = next((p for p in all_props if p.name == key), None)
            if not prop:
                continue
            if prop.type not in ("select", "multiselect") or not prop.content:
                continue
            thesaurus_id = prop.content
            if thesaurus_id not in thesauri_cache:
                thesauri_list = self._thesauri_repo.get(language=language)
                thesaurus = next((t for t in thesauri_list if t.id == thesaurus_id), None)
                if not thesaurus:
                    continue
                label_to_id = {v.label: v.id for v in thesaurus.values}
                valid_ids = {v.id for v in thesaurus.values}
                thesauri_cache[thesaurus_id] = (label_to_id, valid_ids)
            else:
                label_to_id, valid_ids = thesauri_cache[thesaurus_id]
            # values is a list of dicts with "value" key
            for item in values:
                if not isinstance(item, dict) or "value" not in item:
                    continue
                val = item["value"]
                if not isinstance(val, str):
                    continue
                if val in label_to_id:
                    item["value"] = label_to_id[val]
                elif val not in valid_ids and not uuid_pattern.match(val):
                    raise SearchError(f"Value '{val}' not found in thesaurus for property '{key}'")

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
        prop_map = {p.name: p for p in all_props}
        metadata_keys = set(entity.metadata.keys()) if entity.metadata else set()
        for key in entity.metadata or {}:
            if key not in prop_map:
                raise SearchError(f"Metadata property '{key}' not found in template '{template.name}'")
        for prop in all_props:
            if prop.required and prop.name not in metadata_keys:
                raise SearchError(f"Required property '{prop.name}' is missing in entity metadata")
        for key, values in (entity.metadata or {}).items():
            prop = prop_map.get(key)
            if not prop:
                continue
            if not isinstance(values, list):
                raise SearchError(f"Metadata property '{key}' must be a list")
            for item in values:
                if not isinstance(item, dict) or "value" not in item:
                    raise SearchError(f"Metadata property '{key}' items must be objects with 'value' key")
                self._validate_property_value(key, item["value"], prop.type)

    def _validate_property_value(self, key: str, value: Any, prop_type: str) -> None:
        if prop_type in ("text", "markdown", "generatedid"):
            if not isinstance(value, str):
                raise SearchError(
                    f"Metadata property '{key}' ({prop_type}) must have string values, got {type(value).__name__}"
                )
        elif prop_type in ("numeric",):
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise SearchError(
                    f"Metadata property '{key}' (numeric) must have numeric values, got {type(value).__name__}"
                )
        elif prop_type in ("date",):
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise SearchError(
                    f"Metadata property '{key}' (date) must have numeric timestamp values, got {type(value).__name__}"
                )
        elif prop_type in ("daterange", "multidaterange"):
            if not isinstance(value, dict) or "from" not in value or "to" not in value:
                raise SearchError(f"Metadata property '{key}' ({prop_type}) must have objects with 'from' and 'to' keys")
            if not isinstance(value["from"], (int, float)) or not isinstance(value["to"], (int, float)):
                raise SearchError(f"Metadata property '{key}' ({prop_type}) 'from' and 'to' must be numeric timestamps")
        elif prop_type in ("multidate",):
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise SearchError(
                    f"Metadata property '{key}' (multidate) must have numeric timestamp values, got {type(value).__name__}"
                )
        elif prop_type in ("link",):
            if not isinstance(value, dict) or "label" not in value or "url" not in value:
                raise SearchError(f"Metadata property '{key}' (link) must have objects with 'label' and 'url' keys")
        elif prop_type in ("geolocation",):
            if not isinstance(value, dict) or "lat" not in value or "lon" not in value:
                raise SearchError(f"Metadata property '{key}' (geolocation) must have objects with 'lat' and 'lon' keys")
            if not isinstance(value["lat"], (int, float)) or not isinstance(value["lon"], (int, float)):
                raise SearchError(f"Metadata property '{key}' (geolocation) 'lat' and 'lon' must be numeric")
        elif prop_type in ("select", "multiselect"):
            if not isinstance(value, str):
                raise SearchError(
                    f"Metadata property '{key}' ({prop_type}) must have string values (UUID), got {type(value).__name__}"
                )
        elif prop_type in ("image", "media"):
            if not isinstance(value, str) and not (isinstance(value, dict) and "attachment" in value):
                raise SearchError(f"Metadata property '{key}' ({prop_type}) must have string or attachment object values")
        elif prop_type in ("relationship",):
            if not isinstance(value, (str, dict)):
                raise SearchError(f"Metadata property '{key}' (relationship) must have string or object values")

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

    def publish_entities(self, shared_ids: list[str]) -> None:
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

    def delete_entities(self, shared_ids: list[str]) -> None:
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
