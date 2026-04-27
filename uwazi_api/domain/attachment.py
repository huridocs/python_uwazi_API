from pydantic import BaseModel, Field


class Attachment(BaseModel):
    id: str | None = Field(default=None, alias="_id")
    originalname: str = Field(default="", alias="originalname")
