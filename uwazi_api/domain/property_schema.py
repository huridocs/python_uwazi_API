from pydantic import BaseModel


class PropertySchema(BaseModel):
    name: str
    type: str
