from pydantic import BaseModel


class AgentEntityMutationResult(BaseModel):
    shared_id: str
    success: bool
    error: str | None = None
