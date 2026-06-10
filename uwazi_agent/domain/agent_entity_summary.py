from pydantic import BaseModel, Field


class AgentEntitySummary(BaseModel):
    count: int
    by_template: dict[str, int] = Field(default_factory=dict)
    sample_titles: list[str] = Field(default_factory=list)
    shared_ids: list[str] = Field(default_factory=list)
    note: str | None = None
