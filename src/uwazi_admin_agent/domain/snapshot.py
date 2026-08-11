from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class EntitySnapshot(BaseModel):
    """The exact raw entity JSON Uwazi returned for one entity, plus identity.

    ``raw`` is the unmodified dict Uwazi returned; revert restores it verbatim
    (§2.5 raw fidelity - never round-trip through a validated model).
    """

    internal_id: str
    shared_id: str
    language: str | None = None
    captured_at: datetime
    raw: dict[str, Any] = Field(description="Exact raw entity JSON as Uwazi returned it; unmodified.")
