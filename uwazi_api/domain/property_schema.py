from typing import Optional

from pydantic import BaseModel, Field


class PropertySchema(BaseModel):
    id: str = Field(alias="_id")
    name: str
    type: str
    required: bool = False
    filter: bool = False
    content: Optional[str] = None
