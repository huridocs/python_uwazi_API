from typing import Optional

from pydantic import BaseModel, Field

from uwazi_agent.domain.agent_property_type import AgentPropertyType


class AgentProperty(BaseModel):
    name: str
    type: AgentPropertyType
    thesaurus_name: Optional[str] = None
    format_instructions: Optional[str] = None

    use_as_filter: bool = Field(
        default=False,
        description=(
            "Uwazi 'useAsFilter'. When true, this property appears as a sidebar "
            "filter in the library and can be used by the entity filter-search "
            "tool. Only filterable properties can be searched with filters."
        ),
    )
    show_in_card: bool = Field(
        default=False,
        description=(
            "Uwazi 'showInCard'. When true, this property's value is shown on the "
            "entity's summary card in list/search views."
        ),
    )
    required: bool = Field(
        default=False,
        description=(
            "Uwazi 'required'. When true, an entity of this template cannot be saved unless this property has a value."
        ),
    )

    related_template_name: Optional[str] = Field(
        default=None,
        description=(
            "Only for 'relationship' properties. The name of the template whose "
            "entities this property can point to. Leave empty to allow relating to "
            "entities of any template."
        ),
    )
    relationship_type_name: Optional[str] = Field(
        default=None,
        description=(
            "Only for 'relationship' properties. The name of the relationship type "
            "(the labelled connection, e.g. 'author of', 'related to') that links "
            "the two entities. The relationship type must already exist; create it "
            "with create_relationship_type if needed."
        ),
    )
