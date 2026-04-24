from time import sleep
import pandas as pd

from uwazi_api.domain.exceptions import (
    TemplateNotFoundError,
    UploadError,
    SearchError,
)

from uwazi_api.use_cases.repositories.csv_repository import CSVRepository
from uwazi_api.use_cases.repositories.template_repository import TemplateRepository
from uwazi_api.use_cases.repositories.entity_repository import EntityRepository


class CSVUseCase:
    def __init__(
        self,
        csv_repository: "CSVRepository",
        template_repository: "TemplateRepository",
        entity_repository: "EntityRepository",
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

    def upload_dataframe(self, df: pd.DataFrame, template_name: str) -> dict | None:
        template_id = self.template_repo.resolve_template_id(template_name)
        if not template_id:
            raise TemplateNotFoundError(f"Template '{template_name}' not found")
        df_converted = df.copy()
        df_converted = df_converted.apply(lambda col: col.map(self._convert_cell))
        if "_id" in df_converted.columns:
            df_converted = df_converted.drop(columns=["_id"])
        csv_data = df_converted.to_csv(index=False)
        csv_bytes = csv_data.encode("utf-8")
        return self.csv_repo.upload(template_id, csv_bytes)
