from pydantic import BaseModel, Field

from uwazi_api.domain.property_schema import PropertySchema


class Template(BaseModel):
    id: str = Field(alias="_id")
    name: str
    properties: list[PropertySchema] = Field(default_factory=list)
    common_properties: list[PropertySchema] = Field(default_factory=list, alias="commonProperties")

    class Config:
        populate_by_name = True
