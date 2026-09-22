from typing import Any

from pydantic import BaseModel, Field

from uwazi_property_filler.domain.extension_category import ExtensionCategory
from uwazi_property_filler.domain.extension_kind import ExtensionKind


class ExtensionRecord(BaseModel):
    id: str
    name: str
    kind: ExtensionKind
    category: ExtensionCategory
    enabled: bool = True
    endpoint: str | None = None
    entrypoint: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
