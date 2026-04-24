import json
from typing import Any, Optional

from uwazi_api.domain.entity import Entity
from uwazi_api.domain.exceptions import (
    EntityNotFoundError,
    UploadError,
)
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
    ):
        super().__init__(http_client, template_repo=template_repo, thesauri_repo=thesauri_repo)
        self._validator = EntityValidator(template_repo=template_repo, thesauri_repo=thesauri_repo)

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
