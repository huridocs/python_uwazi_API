from typing import Optional

from pydantic import BaseModel, Field


class Attachment(BaseModel):
    id: str | None = Field(default=None, alias="_id")
    originalname: str = Field(default="", alias="originalname")
    filename: str = Field(default="", alias="filename")
    mimetype: str = Field(default="", alias="mimetype")
    size: int = Field(default=0, alias="size")
    creationDate: int = Field(default=0, alias="creationDate")
    entity: str = Field(default="", alias="entity")
    type: str = Field(default="", alias="type")
