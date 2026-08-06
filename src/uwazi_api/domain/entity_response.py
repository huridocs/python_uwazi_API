import pandas as pd
from pydantic import BaseModel, Field

from uwazi_api.domain.entity import Entity


class EntityResponse(BaseModel):
    shared_id: str
    entity: Entity | None
    success: bool
    error: str | None
    traceback: str | None = Field(exclude=True)

    def get_traceback(self) -> str | None:
        return self.traceback

    @staticmethod
    def get_dataframe(df: pd.DataFrame, response_list: list["EntityResponse"]) -> pd.DataFrame:
        df["sharedId"] = [r.shared_id if r.success else None for r in response_list]
        return df
