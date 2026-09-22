from uwazi_property_filler.domain.fill_status import FillStatus
from uwazi_property_filler.domain.highlight import Highlight
from uwazi_property_filler.domain.pdf_item import PdfItem
from uwazi_property_filler.domain.suggestion import Suggestion


def test_fill_status_round_trips() -> None:
    assert FillStatus("pending") is FillStatus.PENDING
    assert FillStatus("validated") is FillStatus.VALIDATED


def test_pdf_item_defaults_to_pending() -> None:
    item = PdfItem(shared_id="abc", template_name="tpl")
    assert item.status is FillStatus.PENDING
    assert item.title == ""
    assert item.filename == ""
    assert item.language == "en"


def test_suggestion_validates() -> None:
    s = Suggestion(property_name="p", value=1, source_extension_id="e")
    assert s.confidence == 1.0
    assert s.highlights == []


def test_highlight_validates() -> None:
    h = Highlight(page=1, left=0.0, top=10.0, width=5.0, height=2.0)
    assert h.text == ""
    assert h.page == 1
