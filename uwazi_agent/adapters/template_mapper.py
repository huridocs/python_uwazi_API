from typing import Optional, Protocol

from uwazi_agent.adapters.property_type_mapper import agent_to_api_property_type, api_to_agent_property_type
from uwazi_agent.domain.agent_property import AgentProperty
from uwazi_agent.domain.agent_property_type import AgentPropertyType
from uwazi_agent.domain.agent_property_type_formats import AGENT_PROPERTY_TYPE_FORMATS
from uwazi_agent.domain.agent_template import AgentTemplate
from uwazi_agent.ports.template_mapper_port import TemplateMapperPort
from uwazi_api.domain.property_schema import PropertySchema
from uwazi_api.domain.property_type import PropertyType
from uwazi_api.domain.template import Template, _default_common_properties


class ThesaurusGateway(Protocol):
    def id_for_name(self, name: str) -> Optional[str]: ...
    def name_for_id(self, thesaurus_id: str) -> Optional[str]: ...


_THESAURUS_TYPES: set[AgentPropertyType] = {AgentPropertyType.SELECT, AgentPropertyType.MULTI_SELECT}
_API_THESAURUS_TYPES: set[PropertyType] = {PropertyType.SELECT, PropertyType.MULTI_SELECT}


class TemplateMapperAdapter(TemplateMapperPort):
    def __init__(self, thesaurus_gateway: Optional[ThesaurusGateway] = None):
        self._thesaurus_gateway = thesaurus_gateway

    def to_agent(self, api_template: Template) -> AgentTemplate:
        agent_props: list[AgentProperty] = []
        for p in api_template.properties:
            agent_type = api_to_agent_property_type(p.type)
            thesaurus_name: Optional[str] = None
            if p.type in _API_THESAURUS_TYPES and p.content and self._thesaurus_gateway is not None:
                thesaurus_name = self._thesaurus_gateway.name_for_id(p.content)
            agent_props.append(
                AgentProperty(
                    name=p.name,
                    type=agent_type,
                    thesaurus_name=thesaurus_name,
                    format_instructions=AGENT_PROPERTY_TYPE_FORMATS.get(agent_type),
                )
            )
        return AgentTemplate(name=api_template.name, properties=agent_props)

    def to_api(self, agent_template: AgentTemplate) -> Template:
        api_props: list[PropertySchema] = []
        for p in agent_template.properties:
            content: Optional[str] = None
            if p.type in _THESAURUS_TYPES and p.thesaurus_name:
                if self._thesaurus_gateway is None:
                    raise ValueError(
                        f"Cannot resolve thesaurus '{p.thesaurus_name}' for property '{p.name}': "
                        "no thesaurus gateway was configured on the mapper"
                    )
                content = self._thesaurus_gateway.id_for_name(p.thesaurus_name)
                if not content:
                    raise ValueError(f"Thesaurus '{p.thesaurus_name}' not found for property '{p.name}'")
            api_props.append(
                PropertySchema(
                    name=p.name,
                    label=p.name,
                    type=agent_to_api_property_type(p.type),
                    content=content,
                )
            )
        return Template(
            name=agent_template.name,
            properties=api_props,
            common_properties=_default_common_properties(),
        )
