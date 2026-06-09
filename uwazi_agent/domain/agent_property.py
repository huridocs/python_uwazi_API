from typing import Optional

from pydantic import BaseModel

from uwazi_agent.domain.agent_property_type import AgentPropertyType


class AgentProperty(BaseModel):
    name: str
    type: AgentPropertyType
    thesaurus_name: Optional[str] = None
