from typing import Any

from pydantic import BaseModel, Field


class AgentEntity(BaseModel):
    shared_id: str
    title: str
    template_name: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    language: str = "en"
    published: bool | None = None


class AgentEntitySearchResult(BaseModel):
    summary: "AgentEntitySummary"
    examples: list[AgentEntity] = Field(default_factory=list)


class AgentEntitySummary(BaseModel):
    count: int
    by_template: dict[str, int] = Field(default_factory=dict)
    sample_titles: list[str] = Field(default_factory=list)
    shared_ids: list[str] = Field(default_factory=list)


class AgentEntityMutationResult(BaseModel):
    shared_id: str
    success: bool
    error: str | None = None
