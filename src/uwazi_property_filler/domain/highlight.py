from pydantic import BaseModel


class Highlight(BaseModel):
    """A text-anchored rectangle in PDF user-space coordinates.

    The geometry matches Uwazi's ``SelectionRectangle`` (the same coordinate
    space segmentation emits), so relationship text references can later
    round-trip.
    """

    page: int
    left: float
    top: float
    width: float
    height: float
    text: str = ""
