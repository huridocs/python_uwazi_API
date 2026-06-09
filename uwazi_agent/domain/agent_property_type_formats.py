from uwazi_agent.domain.agent_property_type import AgentPropertyType


AGENT_PROPERTY_TYPE_FORMATS: dict[AgentPropertyType, str] = {
    AgentPropertyType.TEXT: "plain string",
    AgentPropertyType.MARKDOWN: "markdown string",
    AgentPropertyType.NUMERIC: "int",
    AgentPropertyType.DATE: "`YYYY-MM-DD`",
    AgentPropertyType.DATE_RANGE: "`{from: YYYY-MM-DD, to: YYYY-MM-DD}`",
    AgentPropertyType.MULTI_DATE: '`["YYYY-MM-DD"]` or `["YYYY-MM-DD", "YYYY-MM-DD"]` (for multiple dates)',
    AgentPropertyType.MULTI_DATE_RANGE: "`[{from: YYYY-MM-DD, to: YYYY-MM-DD}]` (for multiple ranges)",
    AgentPropertyType.SELECT: "one of the thesaurus values of the linked thesaurus (label OR id)",
    AgentPropertyType.MULTI_SELECT: "multiple thesaurus values of the linked thesaurus (label OR id)",
    AgentPropertyType.LINK: '`{"label": "text", "url": "href"}`',
    AgentPropertyType.IMAGE: "image URL or file",
    AgentPropertyType.MEDIA: "media URL or file",
    AgentPropertyType.GENERATED_ID: "auto-generated identifier (leave empty to generate)",
    AgentPropertyType.GEO_LOCATION: "`[lat, lon]`, e.g. `[-13.9626, 33.7741]`",
    AgentPropertyType.RELATIONSHIP: "TODO: id of a related entity (not yet supported in agent tools)",
}
