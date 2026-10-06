"""The unified text label: a manual selection or a source prediction.

A ``Label`` anchors a piece of text inside a PDF to a target entity via a Uwazi
relationship. Manual labels and predictions share one shape and differ only in
``source`` (``"manual"`` vs. an extension id) and ``confidence`` (``None`` for
manual). The geometry uses Uwazi's :class:`~uwazi_api.domain.selection_rectangle.SelectionRectangle`
(PDF user-space, bottom-left origin), so a label round-trips through the
relationships endpoint unchanged.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from uwazi_api.domain.selection_rectangle import SelectionRectangle

# ``source`` value for labels the user created by hand (as opposed to a
# prediction emitted by an extension).
MANUAL_SOURCE = "manual"


class Label(BaseModel):
    id: str = ""
    shared_id: str
    file_id: str = ""
    label_shared_id: str
    label_title: str = ""
    relationship_type_id: str = ""
    text: str = ""
    selection_rectangles: list[SelectionRectangle] = Field(default_factory=list)
    page: int = 1
    source: str = MANUAL_SOURCE
    confidence: float | None = None
    # Connection hub ObjectId (Uwazi): present on labels read back from the
    # instance, used to delete the relationship. ``None`` for predictions.
    hub: str | None = None

    @property
    def is_manual(self) -> bool:
        return self.source == MANUAL_SOURCE


class LabelPrediction(BaseModel):
    """A prediction emitted by a single source (extension), before merging.

    Mirrors :class:`Label` minus the persistence-only fields (``hub``); the
    ``source`` is the extension id and ``confidence`` is required.
    """

    label_shared_id: str
    label_title: str = ""
    text: str = ""
    selection_rectangles: list[SelectionRectangle] = Field(default_factory=list)
    page: int = 1
    source: str
    confidence: float = 1.0
