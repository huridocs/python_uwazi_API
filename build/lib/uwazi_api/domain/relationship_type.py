from typing import Optional

from pydantic import BaseModel, Field


class RelationshipType(BaseModel):
    id: Optional[str] = Field(default=None, alias="_id")
    name: str

    class Config:
        populate_by_name = True
