import json
from datetime import datetime
from datetime import timezone
from typing import Any, Optional

import pandas as pd

from uwazi_api.domain.entity import Entity
from uwazi_api.domain.entity_response import EntityResponse
from uwazi_api.domain.exceptions import (
    EntityNotFoundError,
    UploadError,
)
from uwazi_api.domain.document import Document
from uwazi_api.domain.attachment import Attachment
from uwazi_api.adapters.http_client_adapter import HttpClientAdapter
from uwazi_api.use_cases.repositories.search_repository import SearchRepository
from uwazi_api.use_cases.repositories.template_repository import TemplateRepository
from uwazi_api.use_cases.repositories.thesauri_repository import ThesauriRepository
from uwazi_api.use_cases.repositories.entity_validator import EntityValidator


class EntityRepository(SearchRepository):
    def __init__(
        self,
        http_client: HttpClientAdapter,
        template_repo: Optional[TemplateRepository] = None,
        thesauri_repo: Optional[ThesauriRepository] = None,
        validator: Optional[EntityValidator] = None,
    ):
        super().__init__(http_client, template_repo=template_repo, thesauri_repo=thesauri_repo)
        self._validator = validator or EntityValidator(template_repo=template_repo, thesauri_repo=thesauri_repo)

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

        payload = self._validator.validate_and_prepare_for_upload(entity, language)
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

    def update_partially(self, entity: Entity, language: str) -> str:
        if not entity.shared_id:
            raise UploadError("shared_id is required for partial update")

        existing = self.get_one(entity.shared_id, language)

        merged_metadata = existing.metadata.copy()
        merged_metadata.update(entity.metadata)

        merged_entity = Entity(
            _id=existing.id,
            sharedId=existing.shared_id,
            title=entity.title if entity.title is not None else existing.title,
            template=entity.template if entity.template is not None else existing.template,
            language=entity.language if entity.language is not None else existing.language,
            published=entity.published if entity.published is not None else existing.published,
            creationDate=existing.creation_date,
            editDate=existing.edit_date,
            documents=entity.documents if entity.documents else existing.documents,
            attachments=entity.attachments if entity.attachments else existing.attachments,
            metadata=merged_metadata,
        )

        return self.upload(merged_entity, language)

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

    @staticmethod
    def _parse_date_to_timestamp(date_value: Any) -> Optional[float]:
        if pd.isna(date_value):
            return None
        if isinstance(date_value, (int, float)):
            return float(date_value)
        if isinstance(date_value, str):
            date_value = date_value.strip()
            # Try different date formats - treat as UTC to avoid timezone offset issues
            for fmt in ["%Y/%m/%d %H:%M:%S", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"]:
                try:
                    dt = datetime.strptime(date_value, fmt).replace(tzinfo=timezone.utc)
                    return dt.timestamp()
                except ValueError:
                    continue
            # Try parsing as timestamp string
            try:
                return float(date_value)
            except ValueError:
                pass
        return None

    @staticmethod
    def _parse_geolocation(value: Any) -> Optional[dict]:
        if pd.isna(value):
            return None
        value_str = str(value).strip()
        if "|" in value_str:
            parts = value_str.split("|")
            try:
                return {"lat": float(parts[0]), "lon": float(parts[1]), "label": ""}
            except (ValueError, IndexError):
                return None
        return None

    @staticmethod
    def _parse_daterange(value: Any) -> Optional[dict]:
        if pd.isna(value):
            return None
        value_str = str(value).strip()
        # Handle pipe-separated timestamps (from dataframe display)
        if "|" in value_str:
            parts = value_str.split("|")
            try:
                return {"from": float(parts[0]), "to": float(parts[1])}
            except (ValueError, IndexError):
                return None
        # Handle colon-separated dates "2026/04/15:2026/04/29"
        if ":" in value_str:
            parts = value_str.split(":")
            if len(parts) == 2:
                from_ts = EntityRepository._parse_date_to_timestamp(parts[0])
                to_ts = EntityRepository._parse_date_to_timestamp(parts[1])
                if from_ts and to_ts:
                    return {"from": from_ts, "to": to_ts}
        return None

    def _get_prop_type_map(self, template_id: str) -> dict:
        if not self._template_repo or not template_id:
            return {}
        template = self._template_repo.get_by_id(template_id)
        if not template:
            return {}
        all_props = template.properties + template.common_properties
        return {p.name: p.type for p in all_props}

    def _get_name_map(self, template_id: str) -> dict:
        if not self._template_repo or not template_id:
            return {}
        template = self._template_repo.get_by_id(template_id)
        if not template:
            return {}
        all_props = template.properties + template.common_properties
        name_map = {}
        for p in all_props:
            normalized = self._validator._normalize_name(p.name) if self._validator else p.name
            name_map[p.name] = p.name
            name_map[normalized] = p.name
        return name_map

    def create_entities_from_dataframe(self, df: pd.DataFrame, language: str) -> list[EntityResponse]:
        responses = []

        for _, row in df.iterrows():
            try:
                entity_dict = {}

                basic_columns = [
                    "_id",
                    "sharedId",
                    "title",
                    "template",
                    "language",
                    "published",
                    "creationDate",
                    "editDate",
                    "documents",
                    "attachments",
                ]
                metadata_columns = [col for col in row.index if col not in basic_columns and pd.notna(row[col])]

                for col in basic_columns:
                    if col in row and pd.notna(row[col]):
                        if col == "_id":
                            entity_dict["id"] = row[col]
                        elif col == "sharedId":
                            entity_dict["sharedId"] = row[col]
                        elif col == "creationDate":
                            entity_dict["creationDate"] = row[col]
                        elif col == "editDate":
                            entity_dict["editDate"] = row[col]
                        else:
                            entity_dict[col] = row[col]

                if "documents" in entity_dict:
                    doc_filenames = [f.strip() for f in str(entity_dict["documents"]).split("|") if f.strip()]
                    entity_dict["documents"] = [Document(filename=f) for f in doc_filenames]

                if "attachments" in entity_dict:
                    att_filenames = [f.strip() for f in str(entity_dict["attachments"]).split("|") if f.strip()]
                    entity_dict["attachments"] = [Attachment(filename=f) for f in att_filenames]

                # Get template_id for property type mapping
                template_id = row.get("template") if pd.notna(row.get("template")) else None
                prop_type_map = self._get_prop_type_map(template_id) if template_id else {}
                name_map = self._get_name_map(template_id) if template_id else {}

                metadata = {}
                for col in metadata_columns:
                    value = row[col]
                    # Normalize column name to match template property name
                    normalized_col = name_map.get(col, col) if name_map else col
                    prop_type = prop_type_map.get(normalized_col) if prop_type_map else None

                    if prop_type == "date":
                        ts = self._parse_date_to_timestamp(value)
                        if ts is not None:
                            metadata[normalized_col] = ts
                    elif prop_type in ("daterange", "multidaterange"):
                        daterange = self._parse_daterange(value)
                        if daterange:
                            metadata[normalized_col] = daterange
                    elif prop_type == "multidate":
                        if isinstance(value, str) and "|" in value:
                            timestamps = []
                            for v in value.split("|"):
                                ts = self._parse_date_to_timestamp(v)
                                if ts is not None:
                                    timestamps.append(ts)
                            if timestamps:
                                metadata[normalized_col] = timestamps
                        else:
                            ts = self._parse_date_to_timestamp(value)
                            if ts is not None:
                                metadata[normalized_col] = ts
                    elif prop_type in ("select", "multiselect") and isinstance(value, str) and "|" in value:
                        # Split pipe-separated values for select/multiselect
                        metadata[normalized_col] = value.split("|")
                    elif prop_type == "geolocation" and isinstance(value, str):
                        # Convert "lat|lon" to {"lat": ..., "lon": ..., "label": ""}
                        geo = self._parse_geolocation(value)
                        if geo:
                            metadata[normalized_col] = geo
                    else:
                        metadata[normalized_col] = value
                entity_dict["metadata"] = metadata

                entity = Entity(**entity_dict)

                if entity.shared_id:
                    shared_id = self.update_partially(entity, language)
                else:
                    shared_id = self.upload(entity, language)

                responses.append(EntityResponse(shared_id=shared_id, entity=entity, success=True, error=None))

            except Exception as e:
                responses.append(
                    EntityResponse(
                        shared_id=str(row.get("sharedId", "")) if pd.notna(row.get("sharedId")) else "",
                        entity=None,
                        success=False,
                        error=str(e),
                    )
                )

        return responses
