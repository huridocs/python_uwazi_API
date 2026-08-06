from typing import Optional

from pydantic import BaseModel, Field


class AgentSearchFilter(BaseModel):
    """A single filter condition over one filterable template property.

    Provide exactly one kind of condition per filter:

    * ``values`` — for ``select``/``multiselect`` properties: a list of
      thesaurus **labels** (e.g. ``["Malawi", "Zambia"]``). An entity matches
      if it has any of the given values.
    * ``date_from`` / ``date_to`` — for ``date``/``daterange`` properties: an
      inclusive ISO ``YYYY-MM-DD`` lower and/or upper bound. Either or both may
      be set.

    The property must be marked ``use_as_filter`` on its template; inspect the
    template first to confirm and to learn the exact property names.
    """

    property_name: str = Field(description="The template property to filter on (must be use_as_filter).")
    values: Optional[list[str]] = Field(
        default=None,
        description="For select/multiselect: thesaurus labels to match (any-of).",
    )
    date_from: Optional[str] = Field(
        default=None,
        description="For date/daterange: inclusive lower bound, ISO YYYY-MM-DD.",
    )
    date_to: Optional[str] = Field(
        default=None,
        description="For date/daterange: inclusive upper bound, ISO YYYY-MM-DD.",
    )
