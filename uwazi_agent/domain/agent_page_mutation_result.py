from pydantic import BaseModel


class AgentPageMutationResult(BaseModel):
    shared_id: str
    success: bool
    url: str | None = None
    error: str | None = None
