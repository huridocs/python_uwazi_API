import json

from uwazi_api.Reference import Reference
from uwazi_api.UwaziRequest import UwaziRequest


class Relationships:
    def __init__(self, uwazi_request: UwaziRequest):
        self.uwazi_request = uwazi_request

    def create(
        self,
        file_entity_shared_id: str,
        file_id: str,
        reference: Reference,
        to_entity_shared_id: str,
        relationship_type_id: str,
        language: str = "en",
    ):
        relationship_from = {
            "entity": file_entity_shared_id,
            "file": file_id,
            "template": None,
            "reference": reference.to_dict(),
        }

        relationship_to = {
            "entity": to_entity_shared_id,
            "template": relationship_type_id,
        }

        save = [[relationship_from, relationship_to]]
        delete = []

        json_data = {
            "delete": delete,
            "save": save,
        }

        response = self.uwazi_request.request_adapter.post(
            url=f"{self.uwazi_request.url}/api/relationships/bulk",
            headers=self.uwazi_request.headers,
            cookies={"connect.sid": self.uwazi_request.connect_sid, "locale": language},
            data=json.dumps(json_data),
        )

        if response.status_code != 200:
            message = f"Error setting relationships {response.status_code} {response.text}"
            self.uwazi_request.graylog.error(message)
            return None

        self.uwazi_request.graylog.info(f"Relationships set successfully")
        return json.loads(response.text)
