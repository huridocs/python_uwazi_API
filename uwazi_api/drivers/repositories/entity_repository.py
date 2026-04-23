import json
from typing import List, Optional

from uwazi_api.domain.entity import Entity
from uwazi_api.domain.interfaces import EntityRepositoryInterface
from uwazi_api.domain.exceptions import (
    EntityNotFoundError,
    SearchError,
    UploadError,
)
from uwazi_api.drivers.http_client import HttpClient


class EntityRepository(EntityRepositoryInterface):
    def __init__(self, http_client: HttpClient):
        self.http = http_client

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

    def get_shared_ids(self, to_process_template: str, batch_size: int, unpublished: bool = True) -> List[str]:
        params = {
            "_types": f'["{to_process_template}"]',
            "types": f'["{to_process_template}"]',
            "unpublished": "true" if unpublished else "false",
            "limit": batch_size,
            "order": "desc",
            "sort": "creationDate",
        }
        response = self.http.request_adapter.get(
            f"{self.http.url}/api/search",
            headers=self.http.headers,
            params=params,
            cookies={"connect.sid": self.http.connect_sid, "locale": "en"},
        )
        if response.status_code != 200:
            raise SearchError("Error getting entities shared ids")
        rows = json.loads(response.text).get("rows", [])
        return [row["sharedId"] for row in rows]

    def get(
        self,
        start_from: int = 0,
        batch_size: int = 30,
        template_id: Optional[str] = None,
        language: str = "en",
        published: Optional[bool] = None,
    ) -> List[Entity]:
        params = {
            "from": start_from,
            "limit": batch_size,
            "allAggregations": "false",
            "sort": "creationDate",
            "order": "desc",
        }
        if template_id:
            params["types"] = f'["{template_id}"]'
        params["includeUnpublished"] = "false" if published else "true"

        response = self.http.request_adapter.get(
            f"{self.http.url}/api/search",
            headers=self.http.headers,
            params=params,
            cookies={"connect.sid": self.http.connect_sid, "locale": language},
        )
        if response.status_code != 200:
            raise SearchError("Error getting entities")
        rows = json.loads(response.text).get("rows", [])
        return [Entity.model_validate(row) for row in rows]

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

    def upload(self, entity: dict, language: str) -> str:
        upload_response = self.http.request_adapter.post(
            url=f"{self.http.url}/api/entities",
            headers=self.http.headers,
            cookies={"connect.sid": self.http.connect_sid, "locale": language},
            data=json.dumps(entity),
        )
        if upload_response.status_code != 200:
            message = f"Error uploading entity {upload_response.status_code} {upload_response.text}"
            self.http.graylog.error(message)
            raise UploadError(message)
        if "_id" in entity:
            self.http.graylog.info(f"Entity uploaded {entity['_id']}")
        data = json.loads(upload_response.text)
        return data["sharedId"]

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

    def search_by_text(
        self,
        search_term: str,
        template_id: Optional[str] = None,
        start_from: int = 0,
        batch_size: int = 30,
        language: str = "en",
    ) -> List[Entity]:
        params = {
            "allAggregations": "false",
            "from": start_from,
            "includeUnpublished": "true",
            "limit": batch_size,
            "order": "desc",
            "searchTerm": search_term,
            "sort": "_score",
            "treatAs": "number",
            "unpublished": "false",
            "aggregateGeneratedToc": "true",
            "aggregatePublishingStatus": "true",
            "aggregatePermissionsByUsers": "true",
        }
        if template_id:
            params["types"] = f'["{template_id}"]'

        response = self.http.request_adapter.get(
            f"{self.http.url}/api/search",
            headers=self.http.headers,
            params=params,
            cookies={"connect.sid": self.http.connect_sid, "locale": language},
        )
        if response.status_code != 200:
            raise SearchError(f"Error searching entities by text: {response.status_code}")
        rows = json.loads(response.text).get("rows", [])
        return [Entity.model_validate(row) for row in rows]
