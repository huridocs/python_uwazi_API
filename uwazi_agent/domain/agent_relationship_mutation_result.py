from pydantic import BaseModel


class AgentRelationshipMutationResult(BaseModel):
    success: bool
    error: str | None = None
    from_entity_shared_id: str
    to_entity_shared_id: str
    relationship_type_name: str
