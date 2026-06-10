from pydantic import BaseModel, Field


class AgentThesauriGroup(BaseModel):
    """A named group of values inside a thesaurus.

    Groups let a thesaurus organise its values hierarchically: a group has a
    label and a flat list of child value labels. In a ``select``/``multiselect``
    property the children are the selectable options; the group itself is just a
    heading used to organise them.
    """

    name: str
    values: list[str] = Field(default_factory=list)


class AgentThesauri(BaseModel):
    """A controlled vocabulary.

    ``values`` are the top-level (ungrouped) value labels. ``groups`` are named
    groups, each holding its own child value labels. A thesaurus may use either
    or both.
    """

    name: str
    values: list[str] = Field(default_factory=list)
    groups: list[AgentThesauriGroup] = Field(default_factory=list)
