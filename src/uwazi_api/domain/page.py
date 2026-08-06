from typing import Any

from pydantic import BaseModel, Field


class Page(BaseModel):
    id: str | None = Field(default=None, alias="_id")
    shared_id: str | None = Field(default=None, alias="sharedId")
    title: str | None = None
    language: str | None = None
    creation_date: int | None = Field(default=None, alias="creationDate")
    entity_view: bool = Field(default=False, alias="entityView")
    markdown_support: bool = Field(default=False, alias="markdownSupport")
    draft: dict[str, Any] = Field(default_factory=dict)
    releases: list[Any] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    class Config:
        populate_by_name = True
