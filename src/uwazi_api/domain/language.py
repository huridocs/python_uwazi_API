from pydantic import BaseModel


class Language(BaseModel):
    key: str
    label: str = ""
    default: bool = False
