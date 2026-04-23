from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from uwazi_api.domain.attachment import Attachment
from uwazi_api.domain.document import Document


class Entity(BaseModel):
    id: Optional[str] = Field(default=None, alias="_id")
    shared_id: Optional[str] = Field(default=None, alias="sharedId")
    title: Optional[str] = None
    template: Optional[str] = None
    language: Optional[str] = None
    published: Optional[bool] = None
    creation_date: Optional[Any] = Field(default=None, alias="creationDate")
    edit_date: Optional[Any] = Field(default=None, alias="editDate")
    documents: List[Document] = Field(default_factory=list)
    attachments: List[Attachment] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    class Config:
        populate_by_name = True
