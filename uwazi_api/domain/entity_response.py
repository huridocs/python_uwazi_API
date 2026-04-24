from pydantic import BaseModel

from uwazi_api.domain.entity import Entity


class EntityResponse(BaseModel):
    shared_id: str
    entity: Entity | None
    success: bool
    error: str | None = None
