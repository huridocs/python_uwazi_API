from pydantic import BaseModel


class SelectionRectangle(BaseModel):
    top: float
    left: float
    width: float
    height: float
    page: str
