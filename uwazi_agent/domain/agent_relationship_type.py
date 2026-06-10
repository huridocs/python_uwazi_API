from pydantic import BaseModel


class AgentRelationshipType(BaseModel):
    """A labelled kind of connection between two entities.

    Relationship types (Uwazi "relation types") name the meaning of a link,
    e.g. "author of", "cited by", "related to". A ``relationship`` template
    property references one of these to describe what the connection means.
    """

    name: str
