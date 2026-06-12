from pydantic import BaseModel


class AgentRelationshipCreate(BaseModel):
    from_entity_shared_id: str
    to_entity_shared_id: str
    relationship_type_name: str
    file_id: str | None = None
    reference_text: str | None = None
