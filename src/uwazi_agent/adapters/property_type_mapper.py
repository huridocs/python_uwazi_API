from uwazi_agent.domain.agent_property_type import AgentPropertyType
from uwazi_api.domain.property_type import PropertyType


_AGENT_TO_API_TYPE: dict[AgentPropertyType, PropertyType] = {
    AgentPropertyType.TEXT: PropertyType.TEXT,
    AgentPropertyType.DATE: PropertyType.DATE,
    AgentPropertyType.SELECT: PropertyType.SELECT,
    AgentPropertyType.NUMERIC: PropertyType.NUMERIC,
    AgentPropertyType.DATE_RANGE: PropertyType.DATE_RANGE,
    AgentPropertyType.MULTI_DATE: PropertyType.MULTI_DATE,
    AgentPropertyType.LINK: PropertyType.LINK,
    AgentPropertyType.IMAGE: PropertyType.IMAGE,
    AgentPropertyType.MULTI_DATE_RANGE: PropertyType.MULTI_DATE_RANGE,
    AgentPropertyType.MARKDOWN: PropertyType.MARKDOWN,
    AgentPropertyType.MEDIA: PropertyType.MEDIA,
    AgentPropertyType.GENERATED_ID: PropertyType.GENERATED_ID,
    AgentPropertyType.MULTI_SELECT: PropertyType.MULTI_SELECT,
    AgentPropertyType.GEO_LOCATION: PropertyType.GEO_LOCATION,
    AgentPropertyType.RELATIONSHIP: PropertyType.RELATIONSHIP,
    AgentPropertyType.PREVIEW: PropertyType.PREVIEW,
    AgentPropertyType.NESTED: PropertyType.NESTED,
}


def agent_to_api_property_type(agent_type: AgentPropertyType) -> PropertyType:
    return _AGENT_TO_API_TYPE[agent_type]


def api_to_agent_property_type(api_type: PropertyType) -> AgentPropertyType:
    for agent_type, mapped in _AGENT_TO_API_TYPE.items():
        if mapped is api_type:
            return agent_type
    raise ValueError(f"Unknown api PropertyType: {api_type!r}")
