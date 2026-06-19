import json
from typing import Optional

from pydantic import BaseModel, Field

from uwazi_api.domain.property_schema import PropertySchema
from uwazi_api.domain.property_type import PropertyType

from uwazi_api.domain.thesauri import Thesauri


def _default_common_properties() -> list[PropertySchema]:
    return [
        PropertySchema(
            name="title",
            label="Title",
            type=PropertyType.TEXT,
            isCommonProperty=True,
            prioritySorting=False,
        ),
        PropertySchema(
            name="creationDate",
            label="Date added",
            type=PropertyType.DATE,
            isCommonProperty=True,
            prioritySorting=False,
        ),
        PropertySchema(
            name="editDate",
            label="Date modified",
            type=PropertyType.DATE,
            isCommonProperty=True,
        ),
    ]


class Template(BaseModel):
    id: Optional[str] = Field(default=None, alias="_id")
    name: str
    entityViewPage: str = ""
    properties: list[PropertySchema] = Field(default_factory=list)
    common_properties: list[PropertySchema] = Field(default_factory=_default_common_properties, alias="commonProperties")
    color: str = ""

    class Config:
        populate_by_name = True

    def get_schema(self, thesauri: list[Thesauri]) -> str:
        type_value_format = {
            PropertyType.TEXT: "plain string",
            PropertyType.MARKDOWN: "markdown string",
            PropertyType.NUMERIC: "int",
            PropertyType.DATE: "`YYYY-MM-DD`",
            PropertyType.DATE_RANGE: "`{from: YYYY-MM-DD, to: YYYY-MM-DD}`",
            PropertyType.MULTI_DATE: '`["YYYY-MM-DD"]` or `["YYYY-MM-DD", "YYYY-MM-DD"]` (for multiple dates)',
            PropertyType.MULTI_DATE_RANGE: "`[{from: YYYY-MM-DD, to: YYYY-MM-DD}]` (for multiple ranges)",
            PropertyType.SELECT: "one of the thesaurus values below (label OR id)",
            PropertyType.MULTI_SELECT: "multiple thesaurus values below (label OR id)",
            PropertyType.LINK: '`{"label": "text", "url": "href"}`',
            PropertyType.IMAGE: "image URL or file",
            PropertyType.MEDIA: "media URL or file",
            PropertyType.GENERATED_ID: "auto-generated identifier (leave empty to generate)",
            PropertyType.GEO_LOCATION: "`[lat, lon]`, e.g. `[-13.9626, 33.7741]`",
            PropertyType.RELATIONSHIP: "id of a related entity",
            PropertyType.PREVIEW: "no entity value (template-only; decorates the entity view)",
        }

        def _sanitize_column(name: str) -> str:
            return "".join(c if (c.isalnum() or c == "_") else "_" for c in name).strip("_").lower()

        all_properties = self.common_properties + self.properties

        header = (
            "| Property (label) | Sanitized column | Type | Cell value format |\n"
            "|------------------|------------------|------|--------------------|"
        )
        rows = []
        for prop in all_properties:
            label = prop.label or prop.name
            column = _sanitize_column(prop.name) if prop.name else _sanitize_column(label)
            prop_type = prop.type.value
            value_format = type_value_format.get(prop.type, prop_type)
            rows.append(f"| {label} | `{column}` | {prop_type} | {value_format} |")

        schema = f"## Template: {self.name}\n\n## Template schema\n\n{header}\n" + "\n".join(rows)

        if self.color:
            schema += f"\n\n## Template color\n\n`{self.color}`"

        if thesauri:
            thesaurus_lines = [
                "\n\n## Thesaurus Options\nUse the following strict taxonomies to populate select/multiselect fields. You can match either by the exact ID or the exact Label.\n"
            ]
            for thesaurus in thesauri:
                values = [v.label for v in thesaurus.values]
                thesaurus_lines.append(
                    f"\n**{thesaurus.name}**:\n```json\n{json.dumps({thesaurus.name: values}, indent=2)}\n```"
                )
            schema += "\n".join(thesaurus_lines)

        return schema
