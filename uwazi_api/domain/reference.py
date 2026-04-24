from pydantic import BaseModel, Field

from uwazi_api.domain.selection_rectangle import SelectionRectangle


class Reference(BaseModel):
    text: str
    selection_rectangles: list[SelectionRectangle] = Field(default_factory=list, alias="selectionRectangles")

    class Config:
        populate_by_name = True
