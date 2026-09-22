from typing import Any

from pydantic import BaseModel, Field


class ExtensionContext(BaseModel):
    """Request context handed to every extension, for every category."""

    shared_id: str
    template_name: str
    language: str
    filename: str
    pdf_bytes: bytes | None = None
    current_metadata: dict[str, Any] = Field(default_factory=dict)
    properties: list[str] = Field(default_factory=list)
