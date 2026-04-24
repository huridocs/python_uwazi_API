from typing import Any

from pydantic import BaseModel, Field

from uwazi_api.domain.attachment import Attachment
from uwazi_api.domain.document import Document


class Entity(BaseModel):
    id: str | None = Field(default=None, alias="_id")
    shared_id: str | None = Field(default=None, alias="sharedId")
    title: str | None = None
    template: str | None = None
    language: str | None = None
    published: bool | None = None
    creation_date: Any | None = Field(default=None, alias="creationDate")
    edit_date: Any | None = Field(default=None, alias="editDate")
    documents: list[Document] = Field(default_factory=list)
    attachments: list[Attachment] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    class Config:
        populate_by_name = True
