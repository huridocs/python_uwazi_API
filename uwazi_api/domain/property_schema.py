from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator

from uwazi_api.domain.property_type import PropertyType


class PropertyStyle(str, Enum):
    """The ``style`` field on a template property — controls how the UI
    renders the property in the entity view.

    Used by both ``image`` and ``preview`` properties. Other property
    types ignore this field.

    Values map directly to Uwazi's on-disk representation. ``EMPTY``
    is the legacy sentinel stored in older Uwazi templates when the
    user did not pick a style; the validator below normalises it to
    ``COVER`` (the default) on read so the LLM never has to deal
    with it.

    ``COVER`` is the default: omitting ``style`` (or sending the
    legacy empty string) is treated as a cover-style render.
    """

    COVER = "cover"
    FILL = "fill"
    FIT = "fit"
    EMPTY = ""


def _coerce_property_style(value: Any) -> Any:
    """Normalise the on-disk ``style`` field to a ``PropertyStyle`` member.

    Accepts a string (the raw Uwazi payload), ``None`` (legacy unset /
    explicit "no preference"), or an already-coerced ``PropertyStyle``.
    The empty string and ``None`` (legacy "unset" sentinels) are both
    mapped to ``PropertyStyle.COVER`` — the documented default — so
    callers always see a concrete style value.
    """

    if value is None:
        return PropertyStyle.COVER
    if isinstance(value, PropertyStyle):
        return PropertyStyle.COVER if value is PropertyStyle.EMPTY else value
    if isinstance(value, str):
        if value == "":
            return PropertyStyle.COVER
        return PropertyStyle(value)
    return value


class PropertySchema(BaseModel):
    id: Optional[str] = Field(default=None, alias="_id")
    name: str = ""
    label: str = ""
    type: PropertyType
    noLabel: bool = False
    required: bool = False
    showInCard: bool = False
    filter: bool = False
    defaultfilter: bool = False
    prioritySorting: bool = False
    style: Optional[PropertyStyle] = PropertyStyle.COVER
    generatedId: bool = False
    fullWidth: bool = False
    content: Optional[str] = None
    relationType: Optional[str] = None  # Relationship type ID for relationship properties
    isCommonProperty: bool = False

    @model_validator(mode="before")
    @classmethod
    def _coerce_style(cls, data: Any) -> Any:
        if isinstance(data, dict) and "style" in data:
            data = {**data, "style": _coerce_property_style(data["style"])}
        return data
