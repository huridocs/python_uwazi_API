from pydantic import BaseModel, Field

from uwazi_agent.domain.agent_property import AgentProperty


class AgentTemplate(BaseModel):
    name: str
    properties: list[AgentProperty] = Field(default_factory=list)
    color: str = ""
