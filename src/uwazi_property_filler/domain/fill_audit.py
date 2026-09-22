from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class FillAuditRecord(BaseModel):
    instance_key: str
    shared_id: str
    property_name: str
    before_value: Any | None = None
    after_value: Any = None
    extension_ids: list[str] = Field(default_factory=list)
    validated_at: datetime = Field(default_factory=datetime.now)
