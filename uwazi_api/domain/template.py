from typing import List

from pydantic import BaseModel, Field

from uwazi_api.domain.property_schema import PropertySchema


class Template(BaseModel):
    id: str = Field(alias="_id")
    name: str
    properties: List[PropertySchema] = Field(default_factory=list)
    common_properties: List[PropertySchema] = Field(default_factory=list, alias="commonProperties")

    class Config:
        populate_by_name = True
