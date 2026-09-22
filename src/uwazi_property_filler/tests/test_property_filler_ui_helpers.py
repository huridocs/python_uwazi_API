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
