from pydantic import BaseModel, Field

from uwazi_agent.domain.agent_entity import AgentEntity
from uwazi_agent.domain.agent_entity_summary import AgentEntitySummary


class AgentEntitySearchResult(BaseModel):
    summary: AgentEntitySummary
    examples: list[AgentEntity] = Field(default_factory=list)
