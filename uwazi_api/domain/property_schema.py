from typing import Optional

from pydantic import BaseModel, Field

from uwazi_api.domain.property_type import PropertyType


class PropertySchema(BaseModel):
    id: str = Field(alias="_id")
    name: str
    type: PropertyType
    required: bool = False
    filter: bool = False
    content: Optional[str] = None
