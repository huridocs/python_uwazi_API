from typing import Optional

from pydantic import BaseModel, Field

from uwazi_api.domain.property_schema import PropertySchema
from uwazi_api.domain.property_type import PropertyType


def _default_common_properties() -> list[PropertySchema]:
    return [
        PropertySchema(
            name="title",
            label="Title",
            type=PropertyType.TEXT,
            isCommonProperty=True,
            prioritySorting=False,
        ),
        PropertySchema(
            name="creationDate",
            label="Date added",
            type=PropertyType.DATE,
            isCommonProperty=True,
            prioritySorting=False,
        ),
        PropertySchema(
            name="editDate",
            label="Date modified",
            type=PropertyType.DATE,
            isCommonProperty=True,
        ),
    ]


class Template(BaseModel):
    id: Optional[str] = Field(default=None, alias="_id")
    name: str
    entityViewPage: str = ""
    properties: list[PropertySchema] = Field(default_factory=list)
    common_properties: list[PropertySchema] = Field(default_factory=_default_common_properties, alias="commonProperties")
    color: str = ""

    class Config:
        populate_by_name = True
