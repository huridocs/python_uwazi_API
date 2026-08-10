from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, Field


class CssExtraction(BaseModel):
    """Extract a value from supporting-file HTML via a CSS selector."""

    mechanism: Literal["css"] = "css"
    selector: str = Field(description="CSS selector locating the value in the supporting-file HTML.")
    attribute: str | None = Field(
        default=None, description="If set, read this HTML attribute on the matched element; else read its text content."
    )


class RegexExtraction(BaseModel):
    """Extract a value from supporting-file HTML via a regex."""

    mechanism: Literal["regex"] = "regex"
    pattern: str = Field(description="Regex pattern used to extract the value.")
    group: int | str = Field(default=0, description="Capture group index or name to extract (0 = whole match).")


# A discriminated union enforces "exactly one mechanism" structurally: an input
# must declare one of `mechanism: "css"` or `mechaism: "regex"`, and cannot carry
# rwo mechanisms at once
ExtractionSpec: TypeAlias = Annotated[CssExtraction | RegexExtraction, Field(discriminator="mechanism")]
