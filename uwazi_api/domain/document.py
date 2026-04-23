from typing import Optional

from pydantic import BaseModel


class Document(BaseModel):
    filename: str = ""
    language: Optional[str] = None
