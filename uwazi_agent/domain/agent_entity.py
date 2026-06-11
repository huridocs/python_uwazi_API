from typing import Any

from pydantic import BaseModel, Field


class AgentEntity(BaseModel):
    shared_id: str
    title: str
    template_name: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    language: str = "en"
    published: bool | None = None
    creation_date: str | None = None
    edit_date: str | None = None
