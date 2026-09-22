from typing import Any

from pydantic import BaseModel, Field

from uwazi_property_filler.domain.highlight import Highlight


class Suggestion(BaseModel):
    property_name: str
    value: Any
    confidence: float = 1.0
    source_extension_id: str
    highlights: list[Highlight] = Field(default_factory=list)
