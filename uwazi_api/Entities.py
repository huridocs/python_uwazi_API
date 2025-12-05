import json
from typing import Dict, List

import pandas as pd

from uwazi_api.UwaziRequest import UwaziRequest


class Entities:
    def __init__(self, uwazi_request: UwaziRequest):
        self.uwazi_request = uwazi_request

    def get_one(self, shared_id: str, language: str):
        response = self.uwazi_request.request_adapter.get(
            url=f"{self.uwazi_request.url}/api/entities",
            headers=self.uwazi_request.headers,
            cookies={"connect.sid": self.uwazi_request.connect_sid, "locale": language},
            params={"sharedId": shared_id, "omitRelationships": "true"},
        )

        if response.status_code != 200 or len(json.loads(response.text)["rows"]) == 0:
            self.uwazi_request.graylog.error(f"Error getting entity {shared_id} {language}")
            raise InterruptedError(f"Error getting entity {shared_id} {language}")

        return json.loads(response.content.decode("utf-8"))["rows"][0]

    def get_id(self, shared_id: str, language: str):
        entity = self.get_one(shared_id, language)
        return entity["_id"]

    def get_shared_ids(self, to_process_template: str, batch_size: int, unpublished: bool = True):
        params = {
            "_types": f'["{to_process_template}"]',
            "types": f'["{to_process_template}"]',
            "unpublished": "true" if unpublished else "false",
            "limit": batch_size,
            "order": "desc",
            "sort": "creationDate",
        }

        response = self.uwazi_request.request_adapter.get(
            f"{self.uwazi_request.url}/api/search",
            headers=self.uwazi_request.headers,
            params=params,
            cookies={"connect.sid": self.uwazi_request.connect_sid, "locale": "en"},
        )

        if response.status_code != 200:
            raise InterruptedError(f"Error getting entities to update")

        return [json_entity["sharedId"] for json_entity in json.loads(response.text)["rows"]]

    def get(
        self,
        start_from: int = 0,
        batch_size: int = 30,
        template_id: str | None = None,
        language: str = "en",
        published: bool | None = None,
    ):
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

        response = self.uwazi_request.request_adapter.get(
            f"{self.uwazi_request.url}/api/search",
            headers=self.uwazi_request.headers,
            params=params,
            cookies={"connect.sid": self.uwazi_request.connect_sid, "locale": language},
        )

        if response.status_code != 200:
            raise InterruptedError(f"Error getting entities to update")

        return json.loads(response.text)["rows"]

    @staticmethod
    def convert_entities_to_panda(entities):
        flattened_entities = []
        for entity in entities:
            flattened = {
                "_id": entity.get("_id"),
                "sharedId": entity.get("sharedId"),
                "title": entity.get("title"),
                "template": entity.get("template"),
                "language": entity.get("language"),
                "published": entity.get("published"),
                "creationDate": entity.get("creationDate"),
                "editDate": entity.get("editDate"),
            }

            metadata = entity.get("metadata", {})
            for key, value in metadata.items():
                if isinstance(value, list) and len(value) > 0:
                    if isinstance(value[0], dict):
                        extracted_values = []
                        for item in value:
                            if "label" in item:
                                extracted_values.append(item["label"])
                            elif "value" in item:
                                extracted_values.append(item["value"])

                            if "parent" in item and "label" in item["parent"]:
                                extracted_values[-1] = item["parent"]["label"] + "::" + str(extracted_values[-1])

                        if len(extracted_values) == 1:
                            flattened[key] = extracted_values[0]
                        elif len(extracted_values) > 1:
                            flattened[key] = extracted_values
                        else:
                            flattened[key] = None
                    else:
                        flattened[key] = value
                else:
                    flattened[key] = None

            flattened_entities.append(flattened)

        df = pd.DataFrame(flattened_entities)
        return df

    def get_pandas_dataframe(
        self,
        start_from: int = 0,
        batch_size: int = 30,
        template_id: str | None = None,
        language: str = "en",
        published: bool | None = None,
    ) -> pd.DataFrame:
        entities = self.get(start_from, batch_size, template_id, language, published)
        dataframe = self.convert_entities_to_panda(entities)

        response = self.uwazi_request.request_adapter.get(
            url=f"{self.uwazi_request.url}/api/templates",
            headers=self.uwazi_request.headers,
            cookies={"connect.sid": self.uwazi_request.connect_sid},
        )
        template = [t for t in json.loads(response.text)["rows"] if t["_id"] == template_id][0]
        return self.convert_dates(dataframe, template)

    @staticmethod
    def format_timestamp(val, unit):
        return pd.to_datetime(val, unit=unit, errors="coerce").strftime("%Y/%m/%d")

    def parse_daterange(self, val):
        if isinstance(val, dict) and "from" in val and "to" in val:
            return f"{self.format_timestamp(val['from'], 's')}:{self.format_timestamp(val['to'], 's')}"
        return val

    def convert_dates(self, dataframe: pd.DataFrame | None = None, template=None) -> pd.DataFrame | None:
        if dataframe is None or dataframe.empty:
            return dataframe

        date_columns = set()
        daterange_columns = set()
        all_props = template.get("commonProperties", []) + template.get("properties", [])
        for prop in all_props:
            prop_type = prop.get("type")
            prop_name = prop.get("name")
            if prop_name not in dataframe.columns:
                continue
            if prop_type == "date":
                date_columns.add(prop_name)
            elif prop_type == "daterange":
                daterange_columns.add(prop_name)

        df_copy = dataframe.copy()

        for col in date_columns:
            unit = "ms" if col in ["creationDate", "editDate"] else "s"
            pattern = "%Y/%m/%d %H:%M:%S" if col in ["creationDate", "editDate"] else "%Y/%m/%d"
            df_copy[col] = pd.to_datetime(df_copy[col], unit=unit, errors="coerce").dt.strftime(pattern)

        for col in daterange_columns:
            df_copy[col] = df_copy[col].apply(self.parse_daterange)

        return df_copy

    def get_by_id(self, entity_id):
        response = self.uwazi_request.request_adapter.get(
            url=f"{self.uwazi_request.url}/api/entities",
            headers=self.uwazi_request.headers,
            cookies={"connect.sid": self.uwazi_request.connect_sid},
            params={"_id": entity_id, "omitRelationships": "true"},
        )

        if response.status_code != 200 or len(json.loads(response.text)["rows"]) == 0:
            return None

        entity = json.loads(response.text)["rows"][0]
        return entity

    def upload(self, entity: Dict[str, any], language: str):
        upload_response = self.uwazi_request.request_adapter.post(
            url=f"{self.uwazi_request.url}/api/entities",
            headers=self.uwazi_request.headers,
            cookies={"connect.sid": self.uwazi_request.connect_sid, "locale": language},
            data=json.dumps(entity),
        )

        if upload_response.status_code != 200:
            message = f"Error uploading entity {upload_response.status_code} {upload_response.text} {entity}"
            self.uwazi_request.graylog.error(message)
            return

        if "_id" in entity:
            self.uwazi_request.graylog.info(f"Entity uploaded {entity['_id']}")
        shared_id = json.loads(upload_response.text)["sharedId"]
        return shared_id

    def delete(self, share_id: str):
        response = self.uwazi_request.request_adapter.delete(
            f"{self.uwazi_request.url}/api/documents",
            headers=self.uwazi_request.headers,
            params={"sharedId": share_id},
            cookies={"connect.sid": self.uwazi_request.connect_sid},
        )

        if response.status_code != 200:
            print(f"Error ({response.status_code}) deleting entity {share_id}")
            self.uwazi_request.graylog.info(f"Error ({response.status_code}) deleting entity {share_id}")
            return

        print(f"Syncer: Entity deleted {share_id}")
        self.uwazi_request.graylog.info(f"Syncer: Entity deleted {share_id}")

    def publish_entities(self, shared_ids: List[str]):
        entity_new_values = dict()
        entity_new_values["ids"] = shared_ids
        entity_new_values["values"] = {"published": True}

        response = self.uwazi_request.request_adapter.post(
            url=f"{self.uwazi_request.url}/api/entities/multipleupdate",
            headers=self.uwazi_request.headers,
            cookies={"connect.sid": self.uwazi_request.connect_sid},
            data=json.dumps(entity_new_values),
        )
        if response.status_code != 200:
            print(f"Error ({response.status_code}) publishing entities {shared_ids}")
            self.uwazi_request.graylog.info(f"Error ({response.status_code}) publishing entities {shared_ids}")
            return

        print(f"Syncer: Entities published {shared_ids}")
        self.uwazi_request.graylog.info(f"Syncer: Entities published {shared_ids}")

    def delete_entities(self, shared_ids: List[str]):
        entity_new_values = dict()
        entity_new_values["sharedIds"] = shared_ids

        response = self.uwazi_request.request_adapter.post(
            url=f"{self.uwazi_request.url}/api/entities/bulkdelete",
            headers=self.uwazi_request.headers,
            cookies={"connect.sid": self.uwazi_request.connect_sid},
            data=json.dumps(entity_new_values),
        )
        if response.status_code != 200:
            print(f"Error ({response.status_code}) deleting entities {shared_ids}")
            self.uwazi_request.graylog.info(f"Error ({response.status_code}) deleting entities {shared_ids}")
            return

        print(f"Syncer: Entities deleted {shared_ids}")
        self.uwazi_request.graylog.info(f"Syncer: Entities deleted {shared_ids}")

    def get_from_text(
        self,
        search_term: str,
        template_id: str | None = None,
        start_from: int = 0,
        batch_size: int = 30,
        language: str = "en",
    ):
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

        response = self.uwazi_request.request_adapter.get(
            f"{self.uwazi_request.url}/api/search",
            headers=self.uwazi_request.headers,
            params=params,
            cookies={"connect.sid": self.uwazi_request.connect_sid, "locale": language},
        )

        if response.status_code != 200:
            raise InterruptedError(f"Error searching entities by text")

        return json.loads(response.text)["rows"]
