from typing import Optional, Protocol

from uwazi_agent.adapters.property_type_mapper import agent_to_api_property_type, api_to_agent_property_type
from uwazi_agent.domain.agent_property import AgentProperty
from uwazi_agent.domain.agent_property_type import AgentPropertyType
from uwazi_agent.domain.agent_property_type_formats import AGENT_PROPERTY_TYPE_FORMATS
from uwazi_agent.domain.agent_template import AgentTemplate
from uwazi_agent.ports.template_mapper_port import TemplateMapperPort
from uwazi_api.domain.property_schema import PropertySchema, PropertyStyle
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

# Property types that carry a ``style`` UI flag (``fill`` / ``fit``).
_STYLE_TYPES: set[AgentPropertyType] = {AgentPropertyType.IMAGE, AgentPropertyType.PREVIEW}
_API_STYLE_TYPES: set[PropertyType] = {PropertyType.IMAGE, PropertyType.PREVIEW}

# Property types that carry a ``fullWidth`` UI flag.
_FULL_WIDTH_TYPES: set[AgentPropertyType] = {AgentPropertyType.PREVIEW}
_API_FULL_WIDTH_TYPES: set[PropertyType] = {PropertyType.PREVIEW}

_CSS_NAMED_COLORS: dict[str, str] = {
    "aliceblue": "#F0F8FF",
    "antiquewhite": "#FAEBD7",
    "aqua": "#00FFFF",
    "aquamarine": "#7FFFD4",
    "azure": "#F0FFFF",
    "beige": "#F5F5DC",
    "bisque": "#FFE4C4",
    "black": "#000000",
    "blanchedalmond": "#FFEBCD",
    "blue": "#0000FF",
    "blueviolet": "#8A2BE2",
    "brown": "#A52A2A",
    "burlywood": "#DEB887",
    "cadetblue": "#5F9EA0",
    "chartreuse": "#7FFF00",
    "chocolate": "#D2691E",
    "coral": "#FF7F50",
    "cornflowerblue": "#6495ED",
    "cornsilk": "#FFF8DC",
    "crimson": "#DC143C",
    "cyan": "#00FFFF",
    "darkblue": "#00008B",
    "darkcyan": "#008B8B",
    "darkgoldenrod": "#B8860B",
    "darkgray": "#A9A9A9",
    "darkgrey": "#A9A9A9",
    "darkgreen": "#006400",
    "darkkhaki": "#BDB76B",
    "darkmagenta": "#8B008B",
    "darkolivegreen": "#556B2F",
    "darkorange": "#FF8C00",
    "darkorchid": "#9932CC",
    "darkred": "#8B0000",
    "darksalmon": "#E9967A",
    "darkseagreen": "#8FBC8F",
    "darkslateblue": "#483D8B",
    "darkslategray": "#2F4F4F",
    "darkslategrey": "#2F4F4F",
    "darkturquoise": "#00CED1",
    "darkviolet": "#9400D3",
    "deeppink": "#FF1493",
    "deepskyblue": "#00BFFF",
    "dimgray": "#696969",
    "dimgrey": "#696969",
    "dodgerblue": "#1E90FF",
    "firebrick": "#B22222",
    "floralwhite": "#FFFAF0",
    "forestgreen": "#228B22",
    "fuchsia": "#FF00FF",
    "gainsboro": "#DCDCDC",
    "ghostwhite": "#F8F8FF",
    "gold": "#FFD700",
    "goldenrod": "#DAA520",
    "gray": "#808080",
    "grey": "#808080",
    "green": "#008000",
    "greenyellow": "#ADFF2F",
    "honeydew": "#F0FFF0",
    "hotpink": "#FF69B4",
    "indianred": "#CD5C5C",
    "indigo": "#4B0082",
    "ivory": "#FFFFF0",
    "khaki": "#F0E68C",
    "lavender": "#E6E6FA",
    "lavenderblush": "#FFF0F5",
    "lawngreen": "#7CFC00",
    "lemonchiffon": "#FFFACD",
    "lightblue": "#ADD8E6",
    "lightcoral": "#F08080",
    "lightcyan": "#E0FFFF",
    "lightgoldenrodyellow": "#FAFAD2",
    "lightgray": "#D3D3D3",
    "lightgrey": "#D3D3D3",
    "lightgreen": "#90EE90",
    "lightpink": "#FFB6C1",
    "lightsalmon": "#FFA07A",
    "lightseagreen": "#20B2AA",
    "lightskyblue": "#87CEFA",
    "lightslategray": "#778899",
    "lightslategrey": "#778899",
    "lightsteelblue": "#B0C4DE",
    "lightyellow": "#FFFFE0",
    "lime": "#00FF00",
    "limegreen": "#32CD32",
    "linen": "#FAF0E6",
    "magenta": "#FF00FF",
    "maroon": "#800000",
    "mediumaquamarine": "#66CDAA",
    "mediumblue": "#0000CD",
    "mediumorchid": "#BA55D3",
    "mediumpurple": "#9370DB",
    "mediumseagreen": "#3CB371",
    "mediumslateblue": "#7B68EE",
    "mediumspringgreen": "#00FA9A",
    "mediumturquoise": "#48D1CC",
    "mediumvioletred": "#C71585",
    "midnightblue": "#191970",
    "mintcream": "#F5FFFA",
    "mistyrose": "#FFE4E1",
    "moccasin": "#FFE4B4",
    "navajowhite": "#FFDEAD",
    "navy": "#000080",
    "oldlace": "#FDF5E6",
    "olive": "#808000",
    "olivedrab": "#6B8E23",
    "orange": "#FFA500",
    "orangered": "#FF4500",
    "orchid": "#DA70D6",
    "palegoldenrod": "#EEE8AA",
    "palegreen": "#98FB98",
    "paleturquoise": "#AFEEEE",
    "palevioletred": "#DB7093",
    "papayawhip": "#FFEFD5",
    "peachpuff": "#FFDAB9",
    "peru": "#CD853F",
    "pink": "#FFC0CB",
    "plum": "#DDA0DD",
    "powderblue": "#B0E0E6",
    "purple": "#800080",
    "rebeccapurple": "#663399",
    "red": "#FF0000",
    "rosybrown": "#BC8F8F",
    "royalblue": "#4169E1",
    "saddlebrown": "#8B4513",
    "salmon": "#FA8072",
    "sandybrown": "#F4A460",
    "seagreen": "#2E8B57",
    "seashell": "#FFF5EE",
    "sienna": "#A0522D",
    "silver": "#C0C0C0",
    "skyblue": "#87CEEB",
    "slateblue": "#6A5ACD",
    "slategray": "#708090",
    "slategrey": "#708090",
    "snow": "#FFFAFA",
    "springgreen": "#00FF7F",
    "steelblue": "#4682B4",
    "tan": "#D2B48C",
    "teal": "#008080",
    "thistle": "#D8BFD8",
    "tomato": "#FF6347",
    "turquoise": "#40E0D0",
    "violet": "#EE82EE",
    "wheat": "#F5DEB3",
    "white": "#FFFFFF",
    "whitesmoke": "#F5F5F5",
    "yellow": "#FFFF00",
    "yellowgreen": "#9ACD32",
}


def _normalize_color(color: str) -> str:
    if not color:
        return ""
    stripped = color.strip()
    if not stripped:
        return ""
    key = stripped.lower()
    if key in _CSS_NAMED_COLORS:
        return _CSS_NAMED_COLORS[key]
    return stripped


def _find_existing_property(existing: Template, name: str) -> Optional[PropertySchema]:
    for p in existing.common_properties:
        if p.name == name:
            return p
    for p in existing.properties:
        if p.name == name:
            return p
    return None


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
                    style=(p.style if p.type in _API_STYLE_TYPES else None),
                    full_width=(p.fullWidth if p.type in _API_FULL_WIDTH_TYPES else None),
                )
            )
        return AgentTemplate(name=api_template.name, properties=agent_props, color=api_template.color)

    def to_api(
        self,
        agent_template: AgentTemplate,
        existing: Optional[Template] = None,
    ) -> Template:
        api_props: list[PropertySchema] = []
        for p in agent_template.properties:
            content: Optional[str] = None
            relation_type_id: Optional[str] = None

            if p.type in _THESAURUS_TYPES and p.thesaurus_name:
                content = self._resolve_thesaurus(p)
            elif p.type == AgentPropertyType.RELATIONSHIP:
                content, relation_type_id = self._resolve_relationship(p)

            existing_prop = _find_existing_property(existing, p.name) if existing is not None else None
            style_value: Optional[PropertyStyle] = None
            full_width_value: bool = False
            if p.type in _STYLE_TYPES:
                if p.style is not None:
                    style_value = p.style
                elif existing_prop is not None:
                    style_value = existing_prop.style
            if p.type in _FULL_WIDTH_TYPES:
                if p.full_width is not None:
                    full_width_value = p.full_width
                elif existing_prop is not None:
                    full_width_value = existing_prop.fullWidth
            api_props.append(
                PropertySchema(
                    _id=existing_prop.id if existing_prop is not None else None,
                    name=p.name,
                    label=(existing_prop.label if existing_prop is not None and existing_prop.label else p.name),
                    type=agent_to_api_property_type(p.type),
                    content=content,
                    relationType=relation_type_id,
                    filter=p.use_as_filter,
                    showInCard=p.show_in_card,
                    required=p.required,
                    style=style_value,
                    fullWidth=full_width_value,
                )
            )

        common_props: list[PropertySchema] = []
        for default_cp in _default_common_properties():
            existing_cp = _find_existing_property(existing, default_cp.name) if existing is not None else None
            common_props.append(
                PropertySchema(
                    _id=existing_cp.id if existing_cp is not None else default_cp.id,
                    name=default_cp.name,
                    label=(existing_cp.label if existing_cp is not None and existing_cp.label else default_cp.label),
                    type=default_cp.type,
                    isCommonProperty=True,
                    prioritySorting=(existing_cp.prioritySorting if existing_cp is not None else default_cp.prioritySorting),
                )
            )

        return Template(
            name=agent_template.name,
            properties=api_props,
            common_properties=common_props,
            color=_normalize_color(agent_template.color),
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
