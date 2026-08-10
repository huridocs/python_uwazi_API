from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, Field

from uwazi_admin_agent.domain.extraction import ExtractionSpec
from uwazi_admin_agent.domain.filter import EntityFilter


class SetPropertyOp(BaseModel):
    """Set a property to a static value on every entity matching the filter."""

    kind: Literal["set_property"] = "set_property"
    filter: EntityFilter
    property_name: str
    value: str | int | float | bool | list[Any] | dict[str, Any] | None
    allow_overwrite: bool = False


class ExtractFromSupportingFileOp(BaseModel):
    """Extract a value from each matching entity's supporting-file HTML into a property.

    At execution time, an entity must have at least one supporting file or it is
    skipped; this op declares no file selector in the plan language.
    """

    kind: Literal["extract_from_supporting_file"] = "extract_from_supporting_file"
    filter: EntityFilter
    property_name: str
    extraction: ExtractionSpec
    allow_overwrite: bool = False


class RestructureLanguagesOp(BaseModel):
    """Merge one-entity-per-language entitites into one-entity-multi-language.

    This op creates new entities and rewires relationships, so it is the
    motivating case for created-entity tracking and relationship-aware revert.
    Full merge semantics are implemented in Phase 5; the plan language only
    captures the selection and grouping intent.
    """

    kind: Literal["restructure_languages"] = "restructure_languages"
    filter: EntityFilter
    grouping_property: str = Field(description="Identity used to group per-language entities into one.")
    primary_language: str = Field(description="Language of the merged entity's primary sharedId.")


# Closed, extensible op union. New capabilities = a new model here + a `Literal`
# kind + an executor branch - never a "run code" op (§2.1).
Op: TypeAlias = Annotated[SetPropertyOp | ExtractFromSupportingFileOp | RestructureLanguagesOp, Field(discriminator="kind")]
