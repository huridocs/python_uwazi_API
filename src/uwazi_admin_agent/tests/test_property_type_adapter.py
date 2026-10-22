"""Isolated unit tests for the template property-TYPE lookup (per AGENTS.md).

Pure: literal property objects in, a ``{name: type}`` map out. No mocks, no
network, no running instance. The types are what tell the recording path
whether a change is per-language (the free-text types) or one shared value.
"""

from uwazi_admin_agent.adapters.template_property_type_adapter import property_type_map


class _Property:
    """A literal stand-in for the API's property schema (no import needed)."""

    def __init__(self, name: str, type_: str) -> None:
        self.name = name
        self.type = type_


class _EnumType(str):
    """A type that carries its value the way the API's enum does."""

    @property
    def value(self) -> str:
        return str(self)


def test_the_map_carries_each_property_name_and_type() -> None:
    types = property_type_map([_Property("summary", "text"), _Property("languages", "multiselect")])

    assert types == {"summary": "text", "languages": "multiselect"}


def test_an_enum_type_is_read_by_its_value() -> None:
    """The API's property type is a str-Enum; the map must carry the plain name."""
    types = property_type_map([_Property("summary", _EnumType("text"))])

    assert types == {"summary": "text"}


def test_an_unnamed_property_is_skipped() -> None:
    assert property_type_map([_Property("", "text")]) == {}


def test_a_property_without_a_type_is_skipped() -> None:
    """An unknown type must not decide whether a value is per-language."""
    assert property_type_map([_Property("summary", "")]) == {}


def test_no_properties_gives_an_empty_map() -> None:
    assert property_type_map([]) == {}
