from pydantic import BaseModel


class AgentPublishStatus(BaseModel):
    shared_id: str
    published: bool
    permissions: list[dict] = []
