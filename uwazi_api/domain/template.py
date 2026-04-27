from typing import Optional

from pydantic import BaseModel, Field

from uwazi_api.domain.property_schema import PropertySchema


class Template(BaseModel):
    id: Optional[str] = Field(default=None, alias="_id")
    name: str
    entityViewPage: str = ""
    properties: list[PropertySchema] = Field(default_factory=list)
    common_properties: list[PropertySchema] = Field(default_factory=list, alias="commonProperties")
    color: str = ""

    class Config:
        populate_by_name = True
