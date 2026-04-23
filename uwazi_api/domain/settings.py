from typing import List

from pydantic import BaseModel, Field

from uwazi_api.domain.language import Language


class Settings(BaseModel):
    languages: List[Language] = Field(default_factory=list)
