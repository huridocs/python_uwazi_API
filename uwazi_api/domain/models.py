from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class Document(BaseModel):
    filename: str = ""
    language: Optional[str] = None


class Attachment(BaseModel):
    filename: str = ""


class PropertySchema(BaseModel):
    name: str
    type: str


class Template(BaseModel):
    id: str = Field(alias="_id")
    name: str
    properties: List[PropertySchema] = Field(default_factory=list)
    common_properties: List[PropertySchema] = Field(default_factory=list, alias="commonProperties")

    class Config:
        populate_by_name = True


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


class ThesauriValue(BaseModel):
    label: str
    id: str


class Thesauri(BaseModel):
    id: str = Field(alias="_id")
    name: str
    values: List[ThesauriValue] = Field(default_factory=list)

    class Config:
        populate_by_name = True


class Language(BaseModel):
    key: str


class Settings(BaseModel):
    languages: List[Language] = Field(default_factory=list)


class SelectionRectangle(BaseModel):
    top: float
    left: float
    width: float
    height: float
    page: str


class Reference(BaseModel):
    text: str
    selection_rectangles: List[SelectionRectangle] = Field(default_factory=list, alias="selectionRectangles")

    class Config:
        populate_by_name = True
