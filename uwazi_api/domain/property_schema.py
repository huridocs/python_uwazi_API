from typing import Optional

from pydantic import BaseModel, Field

from uwazi_api.domain.property_type import PropertyType


class PropertySchema(BaseModel):
    id: Optional[str] = Field(default=None, alias="_id")
    name: str = ""
    label: str = ""
    type: PropertyType
    noLabel: bool = False
    required: bool = False
    showInCard: bool = False
    filter: bool = False
    defaultfilter: bool = False
    prioritySorting: bool = False
    style: str = ""
    generatedId: bool = False
    content: Optional[str] = None
    relationType: Optional[str] = None  # Relationship type ID for relationship properties
    isCommonProperty: bool = False
