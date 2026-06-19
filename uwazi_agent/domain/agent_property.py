from typing import Optional

from pydantic import BaseModel, Field

from uwazi_agent.domain.agent_property_type import AgentPropertyType
from uwazi_api.domain.property_schema import PropertyStyle


class AgentProperty(BaseModel):
    name: str
    type: AgentPropertyType = Field(
        description=(
            "One of: ``text``, ``markdown``, ``numeric``, ``date``, ``daterange``, "
            "``multidate``, ``multidaterange``, ``select``, ``multiselect``, "
            "``link``, ``image``, ``media``, ``geolocation``, ``relationship``, "
            "``generatedid``, ``preview``. The full list of supported values is "
            "documented in the templates-agent system prompt under 'Supported "
            "property types'; the per-type value shape on entity read/write is "
            "documented in ``AGENT_PROPERTY_TYPE_FORMATS``. Notable entry: "
            "``preview`` is a TEMPLATE-ONLY type that renders the entity's "
            "primary document as an image — there is no per-entity value to "
            "read or write."
        ),
    )
    thesaurus_name: Optional[str] = None
    format_instructions: Optional[str] = None

    use_as_filter: bool = Field(
        default=False,
        description=(
            "Uwazi 'useAsFilter'. When true, this property appears as a sidebar "
            "filter in the library and can be used by the entity filter-search "
            "tool. Only filterable properties can be searched with filters. "
            "search_entities_by_filter will reject any filter whose property_name "
            "is not flagged use_as_filter on the template."
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
    style: Optional[PropertyStyle] = Field(
        default=None,
        description=(
            "Only for ``image`` and ``preview`` properties. How the property is "
            "rendered in the entity view: ``'cover'`` (cover image, the default), "
            "``'fill'`` (fills the container), or ``'fit'`` (fits inside without "
            "cropping). Omit to use ``'cover'``. On an update, omitting it "
            "preserves the property's existing style."
        ),
    )
    full_width: Optional[bool] = Field(
        default=None,
        description=(
            "Only for 'preview' properties. When true, the preview spans the "
            "full width of the entity view (Uwazi 'fullWidth'). Omit to keep "
            "Uwazi's default for the property."
        ),
    )
