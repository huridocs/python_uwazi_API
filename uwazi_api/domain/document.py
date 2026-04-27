from typing import Optional

from pydantic import BaseModel, Field


class Document(BaseModel):
    id: str | None = Field(default=None, alias="_id")
    filename: str = ""
    language: Optional[str] = None
