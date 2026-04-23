import json
from typing import Optional

from uwazi_api.domain.reference import Reference
from uwazi_api.domain.interfaces import RelationshipRepositoryInterface
from uwazi_api.domain.exceptions import UploadError
from uwazi_api.drivers.http_client import HttpClient


class RelationshipRepository(RelationshipRepositoryInterface):
    def __init__(self, http_client: HttpClient):
        self.http = http_client

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
