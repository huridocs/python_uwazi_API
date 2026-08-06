from pydantic import BaseModel, Field

from uwazi_api.domain.language import Language


class Settings(BaseModel):
    languages: list[Language] = Field(default_factory=list)
