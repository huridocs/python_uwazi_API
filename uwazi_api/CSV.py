import json
from io import BytesIO
from time import sleep
from typing import Any

from pandas import DataFrame

from uwazi_api.UwaziRequest import UwaziRequest


class CSV:
    def __init__(self, uwazi_request: UwaziRequest):
        self.uwazi_request = uwazi_request

    def _post_csv(self, template: str, file_content, filename: str = "import.csv"):
        response = self.uwazi_request.request_adapter.post(
            url=f"{self.uwazi_request.url}/api/import",
            data={"template": template},
            files={"file": (filename, file_content, "application/csv")},
            cookies={"connect.sid": self.uwazi_request.connect_sid},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        if response.status_code != 200:
            self.uwazi_request.graylog.info(f"Error uploading CSV", response)
            return f"Error uploading CSV {response.status_code} {response.text}"

        print("CSV uploaded with status ", response.status_code)
        self.uwazi_request.graylog.info(f"CSV uploaded with status {response.status_code}")
        return response

    def upload(self, csv_name: str, template: str):
        csv_path = f"files/csv/{csv_name}.csv"
        with open(csv_path, "rb") as file:
            self._post_csv(template, file)

    @staticmethod
    def _convert_cell(val):
        if val is None or (isinstance(val, float) and val != val):
            return ""
        if isinstance(val, list):
            return "|".join(str(v) for v in val)
        return str(val)

    def upload_dataframe_by_id(self, df: DataFrame, template_id: str):
        df_converted = df.copy()
        df_converted = df_converted.apply(lambda col: col.map(self._convert_cell))
        if "_id" in df_converted.columns:
            df_converted = df_converted.drop(columns=["_id"])
        csv_data = df_converted.to_csv(index=False)
        csv_bytes = BytesIO(csv_data.encode("utf-8"))
        return self._post_csv(template_id, csv_bytes)

    def upload_dataframe(self, df: DataFrame, template_name: str):
        template_id = self.get_template_id(template_name)
        return self.upload_dataframe_by_id(df, template_id)

    def get_template_id(self, template_name: str) -> str:
        response = self.uwazi_request.request_adapter.get(
            url=f"{self.uwazi_request.url}/api/templates",
            headers=self.uwazi_request.headers,
            cookies={"connect.sid": self.uwazi_request.connect_sid},
        )
        template = [t for t in json.loads(response.text)["rows"] if t["name"] == template_name]
        if not template or "_id" not in template[0]:
            raise ValueError(f"Template with name {template_name} not found")
        return template[0]["_id"]

    def upload_dataframe_and_get_shared_id(self, df: DataFrame, template_name: str):
        template_id = self.get_template_id(template_name)
        response = self.upload_dataframe_by_id(df, template_id)
        if response.status_code != 200:
            raise ValueError(f"Error uploading CSV {response.status_code} {response.text}")

        title = df["title"].iloc[0] if "title" in df.columns else None

        if not title:
            raise ValueError("Title column is required in the dataframe to retrieve sharedId")

        for i in range(10):
            entities = self.get_entity_from_text(search_term=title, template_id=None, start_from=0, batch_size=i + 2)
            for entity in entities:
                if entity["title"] == title:
                    return entity["sharedId"]
            sleep(1)

        raise ValueError(f"Error uploading CSV {response.status_code} {response.text}")

    def get_entity_from_text(
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
