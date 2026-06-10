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


class TemplateGateway(Protocol):
    def id_for_name(self, name: str) -> Optional[str]: ...
    def name_for_id(self, template_id: str) -> Optional[str]: ...


class RelationTypeGateway(Protocol):
    def id_for_name(self, name: str) -> Optional[str]: ...
    def name_for_id(self, relation_type_id: str) -> Optional[str]: ...


_THESAURUS_TYPES: set[AgentPropertyType] = {AgentPropertyType.SELECT, AgentPropertyType.MULTI_SELECT}
_API_THESAURUS_TYPES: set[PropertyType] = {PropertyType.SELECT, PropertyType.MULTI_SELECT}


class TemplateMapperAdapter(TemplateMapperPort):
    def __init__(
        self,
        thesaurus_gateway: Optional[ThesaurusGateway] = None,
        template_gateway: Optional[TemplateGateway] = None,
        relation_type_gateway: Optional[RelationTypeGateway] = None,
    ):
        self._thesaurus_gateway = thesaurus_gateway
        self._template_gateway = template_gateway
        self._relation_type_gateway = relation_type_gateway

    def to_agent(self, api_template: Template) -> AgentTemplate:
        agent_props: list[AgentProperty] = []
        for p in api_template.properties:
            agent_type = api_to_agent_property_type(p.type)
            thesaurus_name: Optional[str] = None
            if p.type in _API_THESAURUS_TYPES and p.content and self._thesaurus_gateway is not None:
                thesaurus_name = self._thesaurus_gateway.name_for_id(p.content)

            related_template_name: Optional[str] = None
            relationship_type_name: Optional[str] = None
            if p.type == PropertyType.RELATIONSHIP:
                if p.content and self._template_gateway is not None:
                    related_template_name = self._template_gateway.name_for_id(p.content)
                if p.relationType and self._relation_type_gateway is not None:
                    relationship_type_name = self._relation_type_gateway.name_for_id(p.relationType)

            agent_props.append(
                AgentProperty(
                    name=p.name,
                    type=agent_type,
                    thesaurus_name=thesaurus_name,
                    format_instructions=AGENT_PROPERTY_TYPE_FORMATS.get(agent_type),
                    use_as_filter=p.filter,
                    show_in_card=p.showInCard,
                    required=p.required,
                    related_template_name=related_template_name,
                    relationship_type_name=relationship_type_name,
                )
            )
        return AgentTemplate(name=api_template.name, properties=agent_props)

    def to_api(self, agent_template: AgentTemplate) -> Template:
        api_props: list[PropertySchema] = []
        for p in agent_template.properties:
            content: Optional[str] = None
            relation_type_id: Optional[str] = None

            if p.type in _THESAURUS_TYPES and p.thesaurus_name:
                content = self._resolve_thesaurus(p)
            elif p.type == AgentPropertyType.RELATIONSHIP:
                content, relation_type_id = self._resolve_relationship(p)

            api_props.append(
                PropertySchema(
                    name=p.name,
                    label=p.name,
                    type=agent_to_api_property_type(p.type),
                    content=content,
                    relationType=relation_type_id,
                    filter=p.use_as_filter,
                    showInCard=p.show_in_card,
                    required=p.required,
                )
            )
        return Template(
            name=agent_template.name,
            properties=api_props,
            common_properties=_default_common_properties(),
        )

    def _resolve_thesaurus(self, p: AgentProperty) -> str:
        if self._thesaurus_gateway is None:
            raise ValueError(
                f"Cannot resolve thesaurus '{p.thesaurus_name}' for property '{p.name}': "
                "no thesaurus gateway was configured on the mapper"
            )
        content = self._thesaurus_gateway.id_for_name(p.thesaurus_name)
        if not content:
            raise ValueError(f"Thesaurus '{p.thesaurus_name}' not found for property '{p.name}'")
        return content

    def _resolve_relationship(self, p: AgentProperty) -> tuple[Optional[str], Optional[str]]:
        if not p.relationship_type_name:
            raise ValueError(
                f"Relationship property '{p.name}' requires a 'relationship_type_name'. "
                "List existing types with get_relationship_type_names or create one with "
                "create_relationship_type."
            )
        if self._relation_type_gateway is None:
            raise ValueError(
                f"Cannot resolve relationship type '{p.relationship_type_name}' for property "
                f"'{p.name}': no relation type gateway was configured on the mapper"
            )
        relation_type_id = self._relation_type_gateway.id_for_name(p.relationship_type_name)
        if not relation_type_id:
            raise ValueError(
                f"Relationship type '{p.relationship_type_name}' not found for property '{p.name}'. "
                "Create it first with create_relationship_type."
            )

        content: Optional[str] = None
        if p.related_template_name:
            if self._template_gateway is None:
                raise ValueError(
                    f"Cannot resolve related template '{p.related_template_name}' for property "
                    f"'{p.name}': no template gateway was configured on the mapper"
                )
            content = self._template_gateway.id_for_name(p.related_template_name)
            if not content:
                raise ValueError(f"Related template '{p.related_template_name}' not found for property '{p.name}'.")
        return content, relation_type_id
