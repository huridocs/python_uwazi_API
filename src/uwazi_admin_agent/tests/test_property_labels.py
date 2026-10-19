"""Isolated unit tests for the template property-label lookup (per AGENTS.md).

Pure: literal property objects in, ``{name: label}`` out. No mocks, no I/O, no
running instance — the adapter's mapping is exercised directly, since the lookup
it wraps is a single ``/api/templates`` read (cached behind the client) and has no
logic of its own beyond this projection.
"""

from uwazi_admin_agent.adapters.template_label_adapter import property_label_map


class _Prop:
    """A stand-in for ``uwazi_api.domain.property_schema.PropertySchema``.

    The projection reads ``name``/``label`` off each property, so a literal object
    with those two attributes is the whole contract being tested.
    """

    def __init__(self, name: str, label: str = "") -> None:
        self.name = name
        self.label = label


def test_map_names_properties_to_their_labels() -> None:
    properties = [_Prop("document_ref", "Document Ref"), _Prop("document_type", "Document Type")]

    assert property_label_map(properties) == {"document_ref": "Document Ref", "document_type": "Document Type"}


def test_an_unlabelled_property_maps_to_its_own_name() -> None:
    """Never an empty heading: a nameless label would read as "no property"."""
    assert property_label_map([_Prop("title", "")]) == {"title": "title"}
    assert property_label_map([_Prop("title", "   ")]) == {"title": "title"}


def test_a_blank_name_is_skipped() -> None:
    """A nameless property cannot be looked up, so it is not in the map."""
    assert property_label_map([_Prop("", "Orphan"), _Prop("title", "Title")]) == {"title": "Title"}


def test_an_empty_property_list_is_an_empty_map() -> None:
    assert property_label_map([]) == {}


def test_common_properties_are_included_by_the_caller() -> None:
    """``title`` lives in commonProperties; the caller concatenates both lists."""
    labels = property_label_map([_Prop("document_ref", "Document Ref"), _Prop("title", "Title")])

    assert labels == {"document_ref": "Document Ref", "title": "Title"}
