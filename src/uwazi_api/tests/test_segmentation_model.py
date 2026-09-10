"""Unit tests for the segmentation domain model.

Pure, offline, no mocks: ``Segmentation.model_validate`` is a deterministic
transformation of a literal Uwazi payload into snake_case fields.
"""

from uwazi_api.domain.segmentation import Segmentation

_UWAZI_PAYLOAD = {
    "id": "seg-1",
    "fileId": "file-1",
    "documentId": "doc-1",
    "filename": "english_testing_file.pdf",
    "status": "ready",
    "xmlname": "english_testing_file.xml",
    "pageHeight": 841,
    "pageWidth": 595,
    "paragraphs": [
        {
            "left": 58,
            "top": 63,
            "width": 457,
            "height": 15,
            "pageNumber": 1,
            "text": "A sample paragraph from segmentation",
            "type": "paragraph",
        }
    ],
}


def test_parses_uwazi_segmentation_payload() -> None:
    segmentation = Segmentation.model_validate(_UWAZI_PAYLOAD)

    assert segmentation.id == "seg-1"
    assert segmentation.file_id == "file-1"
    assert segmentation.document_id == "doc-1"
    assert segmentation.filename == "english_testing_file.pdf"
    assert segmentation.status == "ready"
    assert segmentation.xmlname == "english_testing_file.xml"
    assert segmentation.page_width == 595
    assert segmentation.page_height == 841

    assert len(segmentation.paragraphs) == 1
    paragraph = segmentation.paragraphs[0]
    assert paragraph.left == 58
    assert paragraph.top == 63
    assert paragraph.width == 457
    assert paragraph.height == 15
    assert paragraph.page_number == 1
    assert paragraph.text == "A sample paragraph from segmentation"
    assert paragraph.type == "paragraph"


def test_defaults_tolerate_missing_optionals() -> None:
    segmentation = Segmentation.model_validate(
        {
            "id": "seg-2",
            "fileId": "file-2",
            "documentId": "doc-2",
            "filename": "doc.pdf",
            "status": "ready",
        }
    )

    assert segmentation.xmlname is None
    assert segmentation.page_width is None
    assert segmentation.page_height is None
    assert segmentation.paragraphs == []
