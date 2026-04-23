from io import BytesIO
from time import sleep
from typing import Optional

import pandas as pd

from uwazi_api.domain import (
    CSVRepositoryInterface,
    EntityRepositoryInterface,
    TemplateRepositoryInterface,
    TemplateNotFoundError,
    UploadError,
    SearchError,
)


class CSVImportUseCase:
    def __init__(
        self,
        csv_repository: CSVRepositoryInterface,
        template_repository: TemplateRepositoryInterface,
        entity_repository: EntityRepositoryInterface,
    ):
        self.csv_repo = csv_repository
        self.template_repo = template_repository
        self.entity_repo = entity_repository

    @staticmethod
    def _convert_cell(val):
        if val is None or (isinstance(val, float) and val != val):
            return ""
        if isinstance(val, list):
            return "|".join(str(v) for v in val)
        return str(val)

    def upload_dataframe_by_id(self, df: pd.DataFrame, template_id: str) -> dict:
        df_converted = df.copy()
        df_converted = df_converted.apply(lambda col: col.map(self._convert_cell))
        if "_id" in df_converted.columns:
            df_converted = df_converted.drop(columns=["_id"])
        csv_data = df_converted.to_csv(index=False)
        csv_bytes = csv_data.encode("utf-8")
        return self.csv_repo.upload(template_id, csv_bytes)

    def upload_dataframe(self, df: pd.DataFrame, template_name: str) -> dict:
        template = self.template_repo.get_by_name(template_name)
        if not template:
            raise TemplateNotFoundError(f"Template with name {template_name} not found")
        return self.upload_dataframe_by_id(df, template.id)

    def upload_dataframe_and_get_shared_id(self, df: pd.DataFrame, template_name: str) -> str:
        template = self.template_repo.get_by_name(template_name)
        if not template:
            raise TemplateNotFoundError(f"Template with name {template_name} not found")
        response = self.upload_dataframe_by_id(df, template.id)
        if response.get("status_code") != 200:
            raise UploadError(f"Error uploading CSV {response.get('status_code')} {response.get('text')}")

        title = df["title"].iloc[0] if "title" in df.columns else None
        if not title:
            raise ValueError("Title column is required in the dataframe to retrieve sharedId")

        for i in range(10):
            entities = self.entity_repo.search_by_text(
                search_term=title, template_id=None, start_from=0, batch_size=i + 2
            )
            for entity in entities:
                if entity.title == title:
                    return entity.shared_id
            sleep(1)

        raise SearchError(f"Could not find uploaded entity with title '{title}'")
