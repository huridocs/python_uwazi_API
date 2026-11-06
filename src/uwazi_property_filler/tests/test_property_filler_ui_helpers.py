"""Isolated unit tests for the property-filler pure helpers (no I/O, no mocks)."""

from uwazi_property_filler.domain.fill_status import FillStatus
from uwazi_property_filler.domain.metadata_text import metadata_text, metadata_value, relationship_entries
from uwazi_property_filler.domain.pdf_item import PdfItem


def test_metadata_text_scalar() -> None:
    assert metadata_text("DOC-1") == "DOC-1"


def test_metadata_text_none_and_empty() -> None:
    assert metadata_text(None) == ""
    assert metadata_text("") == ""
    assert metadata_text([None, "", "   "]) == ""


def test_metadata_text_value_envelope() -> None:
    assert metadata_text([{"value": "DOC-2"}]) == "DOC-2"


def test_metadata_text_nested_envelope_takes_label() -> None:
    # Link-style cell: {"value": {"label": ..., "url": ...}}
    assert metadata_text([{"value": {"label": "L", "url": "http://u"}}]) == "L"


def test_metadata_text_first_non_empty_wins() -> None:
    assert metadata_text([None, {"label": "DOC-3"}, "later"]) == "DOC-3"


def test_metadata_text_plain_list_first_item() -> None:
    assert metadata_text(["a", "b"]) == "a"


def test_metadata_text_strips_whitespace() -> None:
    assert metadata_text("  DOC-4  ") == "DOC-4"


def test_pdf_item_subtitle_defaults_and_round_trip() -> None:
    item = PdfItem(
        shared_id="abc",
        title="Resolution 4",
        subtitle="A/RES/4",
        template_name="DOCUMENT",
        filename="res.pdf",
    )
    assert item.subtitle == "A/RES/4"
    assert item.status is FillStatus.PENDING
    dumped = item.model_dump()
    assert PdfItem(**dumped).subtitle == "A/RES/4"


def test_metadata_value_exact_key() -> None:
    assert metadata_value({"document_ref": "A/RES/4"}, "document_ref") == "A/RES/4"


def test_metadata_value_matches_label_key() -> None:
    # Uwazi may key search metadata by the property label instead of its name.
    assert metadata_value({"Document Ref": "A/RES/4"}, "document_ref") == "A/RES/4"


def test_metadata_value_matches_camel_case_key() -> None:
    assert metadata_value({"documentRef": "A/RES/4"}, "document_ref") == "A/RES/4"


def test_metadata_value_configured_as_label() -> None:
    # `.env` carries a label; the metadata dict carries the sanitized name.
    assert metadata_value({"document_ref": "A/RES/4"}, "Document Ref") == "A/RES/4"


def test_metadata_value_missing_property_returns_none() -> None:
    assert metadata_value({"document_type": ["Resolutions"]}, "document_ref") is None


def test_metadata_value_disabled_when_unconfigured() -> None:
    assert metadata_value({"document_ref": "A/RES/4"}, "") is None


def test_metadata_value_empty_metadata_returns_none() -> None:
    assert metadata_value({}, "document_ref") is None


def test_metadata_value_feeds_metadata_text() -> None:
    cell = metadata_value({"Document Ref": [{"value": "A/RES/4"}]}, "document_ref")
    assert metadata_text(cell) == "A/RES/4"


def test_relationship_entries_from_agent_shape() -> None:
    value = [{"shared_id": "abc", "title": "Human rights"}, {"shared_id": "def", "title": "Environment"}]
    assert relationship_entries(value) == [
        {"shared_id": "abc", "title": "Human rights"},
        {"shared_id": "def", "title": "Environment"},
    ]


def test_relationship_entries_single_item_and_envelope() -> None:
    assert relationship_entries({"shared_id": "abc", "title": "T"}) == [{"shared_id": "abc", "title": "T"}]
    assert relationship_entries([{"value": "abc"}]) == [{"shared_id": "abc", "title": "abc"}]
    assert relationship_entries("abc") == [{"shared_id": "abc", "title": "abc"}]


def test_relationship_entries_title_falls_back_to_shared_id() -> None:
    assert relationship_entries([{"shared_id": "abc"}]) == [{"shared_id": "abc", "title": "abc"}]


def test_relationship_entries_drops_empty_entries() -> None:
    assert relationship_entries(None) == []
    assert relationship_entries([]) == []
    assert relationship_entries([{}, {"title": "no id"}, "", None]) == []


def test_relationship_entries_single_string_in_list() -> None:
    assert relationship_entries(["abc"]) == [{"shared_id": "abc", "title": "abc"}]


# --- resolving the configured fill property --------------------------------
# PROPERTY_FILLER_PROPERTY may hold the internal name ("keywords") or the label
# ("Keywords") an operator copies from Uwazi. An exact name match made the
# "Current value" section render "Property not found" for a label setting.


def _props():
    from uwazi_api.domain.property_schema import PropertySchema
    from uwazi_api.domain.property_type import PropertyType

    return [
        PropertySchema(name="keywords", label="Keywords", type=PropertyType.MULTI_SELECT, content="th1"),
        PropertySchema(name="document_type", label="Document Type", type=PropertyType.SELECT, content="th2"),
        PropertySchema(name="document_ref_no", label="Document Ref. No.", type=PropertyType.TEXT),
    ]


def test_fill_property_resolves_from_its_label() -> None:
    from uwazi_property_filler.drivers.web import app

    assert app._resolve_fill_property(_props(), "Keywords").name == "keywords"


def test_fill_property_resolves_from_its_internal_name() -> None:
    from uwazi_property_filler.drivers.web import app

    assert app._resolve_fill_property(_props(), "keywords").name == "keywords"


def test_fill_property_resolution_ignores_case_and_punctuation() -> None:
    """``"Document Ref. No."`` must match ``document_ref_no``."""
    from uwazi_property_filler.drivers.web import app

    assert app._resolve_fill_property(_props(), "DOCUMENT REF NO").name == "document_ref_no"


def test_fill_property_unknown_setting_resolves_to_none() -> None:
    from uwazi_property_filler.drivers.web import app

    assert app._resolve_fill_property(_props(), "nope") is None
    assert app._resolve_fill_property(_props(), "") is None


def test_fill_property_skips_structural_types() -> None:
    from uwazi_api.domain.property_schema import PropertySchema
    from uwazi_api.domain.property_type import PropertyType
    from uwazi_property_filler.drivers.web import app

    props = [PropertySchema(name="preview", label="Preview", type=PropertyType.PREVIEW)]
    assert app._resolve_fill_property(props, "Preview") is None


def test_suggestion_property_matches_by_name_or_label() -> None:
    """A static suggestion row defaults to the label; the property carries the name."""
    from uwazi_property_filler.drivers.web import app

    def kept(prop_name: str, prop) -> bool:
        return not (
            app._key_form(prop_name) != app._key_form(prop.name)
            and app._key_form(prop_name) != app._key_form(prop.label or "")
        )

    prop = _props()[0]
    assert kept("Keywords", prop)
    assert kept("keywords", prop)
    assert not kept("Document Type", prop)


def test_entity_url_points_at_the_entity_page() -> None:
    from uwazi_property_filler.drivers.web import app

    assert app._entity_url("qt7fau6bu7p") == f"{app._CONTROLLED_UWAZI_URL.rstrip('/')}/entity/qt7fau6bu7p"
