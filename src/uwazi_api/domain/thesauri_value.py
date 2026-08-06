from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class ThesauriValue(BaseModel):
    label: str
    id: str
    values: Optional[list["ThesauriValue"]] = Field(default_factory=list)
