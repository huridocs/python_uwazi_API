from typing import List

from pydantic import BaseModel, Field

from uwazi_api.domain.thesauri_value import ThesauriValue


class Thesauri(BaseModel):
    id: str = Field(alias="_id")
    name: str
    values: List[ThesauriValue] = Field(default_factory=list)

    class Config:
        populate_by_name = True
