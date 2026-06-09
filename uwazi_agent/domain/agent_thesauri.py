from pydantic import BaseModel, Field


class AgentThesauri(BaseModel):
    name: str
    values: list[str] = Field(default_factory=list)
