import pandas as pd
from pydantic import BaseModel

from uwazi_api.domain.entity import Entity


class EntityResponse(BaseModel):
    shared_id: str
    entity: Entity | None
    success: bool
    error: str | None = None

    @staticmethod
    def get_dataframe(df: pd.DataFrame, response_list: list[EntityResponse]) -> pd.DataFrame:
        df["sharedId"] = [r.shared_id if r.success else None for r in response_list]
        return df
