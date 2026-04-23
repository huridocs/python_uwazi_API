from typing import List

from pydantic import BaseModel


class ThesauriValue(BaseModel):
    label: str
    id: str
