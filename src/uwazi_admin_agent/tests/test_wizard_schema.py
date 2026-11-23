"""Isolated tests for the guided wizard's instance-schema projection.

Per ``AGENTS.md``: no mocks, no network. These build real ``uwazi_api`` model
objects and exercise the pure projection that turns them into the wizard's
option data.
"""

from uwazi_admin_agent.drivers.web.run_service.wizard_schema import wizard_schema_from_models
from uwazi_api.domain.language import Language
from uwazi_api.domain.property_schema import PropertySchema
from uwazi_api.domain.property_type import PropertyType
from uwazi_api.domain.template import Template


def _template(name: str, *properties: PropertySchema) -> Template:
    return Template(name=name, properties=list(properties))


def test_projection_keeps_common_properties_and_drops_non_value_types() -> None:
    template = _template(
        "Judgment",
        PropertySchema(name="judge", label="Judge", type=PropertyType.TEXT),
        PropertySchema(name="cover", label="Cover", type=PropertyType.PREVIEW),
        PropertySchema(name="group", label="Group", type=PropertyType.NESTED),
    )
    schema = wizard_schema_from_models([template], [Language(key="en", label="English", default=True)])
    names = [prop.name for prop in schema.templates[0].properties]
    assert names == ["judge", "title", "creationDate", "editDate"]
    assert schema.languages[0].key == "en" and schema.languages[0].default


def test_projection_skips_unnamed_properties_and_templates_sort_by_name() -> None:
    schema = wizard_schema_from_models(
        [
            _template("Order"),
            _template("Judgment", PropertySchema(name="", label="Untitled", type=PropertyType.TEXT)),
        ],
        [],
    )
    assert [template.name for template in schema.templates] == ["Judgment", "Order"]
    # Only the template's own common properties remain; the unnamed one is gone.
    assert [prop.name for prop in schema.templates[0].properties] == ["title", "creationDate", "editDate"]


def test_projection_tolerates_missing_languages() -> None:
    """An unreadable language list degrades to no labels, never to an error."""
    schema = wizard_schema_from_models(
        [_template("Judgment")],
        [Language(key=""), Language(key="es", label="Spanish")],
    )
    assert [language.key for language in schema.languages] == ["es"]
