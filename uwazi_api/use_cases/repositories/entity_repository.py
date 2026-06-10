import json
import traceback
from typing import Optional

import pandas as pd

from uwazi_api.domain.entity import Entity
from uwazi_api.domain.entity_response import EntityResponse
from uwazi_api.domain.exceptions import (
    EntityNotFoundError,
    UploadError,
)
from uwazi_api.domain.dataframe_entity_mapper import DataFrameEntityMapper
from uwazi_api.adapters.http_client_adapter import HttpClientAdapter
from uwazi_api.use_cases.repositories.search_repository import SearchRepository
from uwazi_api.use_cases.repositories.template_repository import TemplateRepository
from uwazi_api.use_cases.repositories.thesauri_repository import ThesauriRepository
from uwazi_api.use_cases.repositories.entity_validator import EntityValidator
from uwazi_api.use_cases.sanitize_property_label import PropertyLabelSanitizer


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
            cookies={"locale": language},
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
            cookies={},
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
            cookies={"locale": language},
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
            cookies={},
        )
        if response.status_code != 200:
            message = f"Error ({response.status_code}) deleting entity {shared_id}"
            self.http.graylog.info(message)
            raise UploadError(message)
        self.http.graylog.info(f"Entity deleted {shared_id}")

    def publish_entities(self, shared_ids: list[str], permissions: Optional[list[dict]] = None) -> None:
        if permissions is None:
            permissions = [
                {
                    "refId": "public",
                    "type": "public",
                    "level": "read",
                },
            ]
        payload = {"ids": shared_ids, "permissions": permissions}
        response = self.http.request_adapter.post(
            url=f"{self.http.url}/api/entities/permissions",
            headers=self.http.headers,
            cookies={},
            data=json.dumps(payload),
        )
        if response.status_code != 200:
            message = f"Error ({response.status_code}) publishing entities {shared_ids}"
            self.http.graylog.info(message)
            raise UploadError(message)
        self.http.graylog.info(f"Entities published {shared_ids}")

    def unpublish_entities(self, shared_ids: list[str]) -> None:
        payload = {"ids": shared_ids, "permissions": []}
        response = self.http.request_adapter.post(
            url=f"{self.http.url}/api/entities/permissions",
            headers=self.http.headers,
            cookies={},
            data=json.dumps(payload),
        )
        if response.status_code != 200:
            message = f"Error ({response.status_code}) unpublishing entities {shared_ids}"
            self.http.graylog.info(message)
            raise UploadError(message)
        self.http.graylog.info(f"Entities unpublished {shared_ids}")

    def delete_entities(self, shared_ids: list[str]) -> None:
        payload = {"sharedIds": shared_ids}
        response = self.http.request_adapter.post(
            url=f"{self.http.url}/api/entities/bulkdelete",
            headers=self.http.headers,
            cookies={},
            data=json.dumps(payload),
        )
        if response.status_code != 200:
            message = f"Error ({response.status_code}) deleting entities {shared_ids}"
            self.http.graylog.info(message)
            raise UploadError(message)
        self.http.graylog.info(f"Entities deleted {shared_ids}")

    def _resolve_template(self, template_name_or_id: str):
        if not self._template_repo or not template_name_or_id:
            return None
        template = self._template_repo.get_by_id(template_name_or_id)
        if template:
            return template
        return self._template_repo.get_by_name(template_name_or_id)

    def _get_prop_type_map(self, template_name_or_id: str) -> dict:
        template = self._resolve_template(template_name_or_id)
        if not template:
            return {}
        all_props = template.properties + template.common_properties
        return {p.name: p.type for p in all_props}

    def _get_name_map(self, template_name_or_id: str) -> dict:
        template = self._resolve_template(template_name_or_id)
        if not template:
            return {}
        all_props = template.properties + template.common_properties
        name_map = {}
        for p in all_props:
            normalized = PropertyLabelSanitizer.sanitize(p.name)
            name_map[p.name] = p.name
            name_map[normalized] = p.name
            if p.type == "geolocation":
                if not normalized.endswith("_geolocation"):
                    name_map[f"{normalized}_geolocation"] = p.name
                if not p.name.endswith("_geolocation"):
                    name_map[f"{p.name}_geolocation"] = p.name
        return name_map

    def _check_duplicate_sanitized_columns(self, df: pd.DataFrame) -> list[str]:
        default_cols = set(DataFrameEntityMapper.BASIC_COLUMNS)
        sanitized_map: dict[str, list[str]] = {}
        for col in df.columns:
            if col in default_cols:
                continue
            is_geolocation = col.endswith("_geolocation")
            original_col = col[: -len("_geolocation")] if is_geolocation else col
            sanitized = PropertyLabelSanitizer.sanitize(original_col)
            sanitized = sanitized + "_geolocation" if is_geolocation and sanitized else sanitized
            if not sanitized:
                sanitized = col
            if sanitized not in sanitized_map:
                sanitized_map[sanitized] = []
            sanitized_map[sanitized].append(col)
        duplicates = []
        for sanitized, original_cols in sanitized_map.items():
            if len(original_cols) > 1:
                duplicates.append(f"'{sanitized}' from columns: {original_cols}")
        return duplicates

    def create_or_update_entities_from_dataframe(
        self, df: pd.DataFrame, language: str, template: str = ""
    ) -> list[EntityResponse]:
        responses = []

        duplicate_columns = self._check_duplicate_sanitized_columns(df)
        if duplicate_columns:
            return [
                EntityResponse(
                    shared_id="",
                    entity=None,
                    success=False,
                    error=f"Duplicate column names after sanitization detected: {', '.join(duplicate_columns)}",
                    traceback=None,
                )
            ]

        df = DataFrameEntityMapper.sanitize_dataframe(df, template)

        for _, row in df.iterrows():
            try:
                template_id = row.get("template") if pd.notna(row.get("template")) else None
                prop_type_map = self._get_prop_type_map(template_id) if template_id else {}
                name_map = self._get_name_map(template_id) if template_id else {}

                mapper = DataFrameEntityMapper(prop_type_map=prop_type_map, name_map=name_map)
                entity = mapper.map_row_to_entity(row)

                shared_id = self.update_partially(entity, language) if entity.shared_id else self.upload(entity, language)
                responses.append(
                    EntityResponse(shared_id=shared_id, entity=entity, success=True, error=None, traceback=None)
                )

            except Exception as e:
                responses.append(
                    EntityResponse(
                        shared_id=str(row.get("sharedId", "")) if pd.notna(row.get("sharedId")) else "",
                        entity=None,
                        success=False,
                        error=f"{str(e)}",
                        traceback=f"{str(e)}\n{traceback.format_exc()}",
                    )
                )

        return responses
