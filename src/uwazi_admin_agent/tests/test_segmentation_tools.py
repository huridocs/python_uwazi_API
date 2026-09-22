"""Isolated unit tests for the segmentation formatting helper (per AGENTS.md).

Pure tests only: real :class:`Segmentation` / :class:`Paragraph` inputs, real
``format_segmentation``, no mocks, no network, no repository ports, no running
Uwazi instance.
"""

from uwazi_admin_agent.use_cases.segmentation_tools import format_segmentation
from uwazi_api.domain.segmentation import Paragraph, Segmentation


def _seg(paragraphs: list[Paragraph], filename: str = "doc.pdf") -> Segmentation:
    return Segmentation(
        id="seg-1",
        file_id="file-1",
        document_id="doc-1",
        filename=filename,
        status="ready",
        paragraphs=paragraphs,
    )


def _para(page_number: int, top: float, text: str) -> Paragraph:
    return Paragraph(
        left=0.0,
        top=top,
        width=10.0,
        height=10.0,
        pageNumber=page_number,
        text=text,
        type="paragraph",
    )


def test_format_segmentation_renders_header_and_paragraph_lines() -> None:
    seg = _seg([_para(1, 0.0, "first"), _para(1, 15.0, "second")])

    out = format_segmentation(seg)

    assert out.startswith("file: doc.pdf (status=ready, pages=1, paragraphs=2)")
    assert "[page 1] first" in out
    assert "[page 1] second" in out


def test_format_segmentation_reports_page_count_as_highest_page_number() -> None:
    """The header's page count is the max paragraph page_number (pages are
    1-indexed), so 'how many pages' is answerable without scanning the body."""
    seg = _seg([_para(1, 0.0, "one"), _para(3, 0.0, "three"), _para(2, 0.0, "two")])

    out = format_segmentation(seg)

    assert out.startswith("file: doc.pdf (status=ready, pages=3, paragraphs=3)")


def test_format_segmentation_orders_by_page_then_top() -> None:
    """Paragraphs are emitted in natural reading order regardless of input order."""
    seg = _seg(
        [
            _para(2, 0.0, "page two"),
            _para(1, 15.0, "page one bottom"),
            _para(1, 0.0, "page one top"),
        ]
    )

    out = format_segmentation(seg)

    assert out.index("page one top") < out.index("page one bottom") < out.index("page two")


def test_format_segmentation_empty_paragraphs_renders_header_only() -> None:
    out = format_segmentation(_seg([]))

    assert out == "file: doc.pdf (status=ready, pages=0, paragraphs=0)"


def test_format_segmentation_truncates_long_text_keeping_head_and_tail() -> None:
    """A very large segmentation is truncated head-and-tail while the first and
    last paragraph lines survive (mirrors peek_file_text truncation)."""
    head_text = "HEADPARA"
    tail_text = "TAILPARA"
    middle = [_para(1, float(i), "x" * 100) for i in range(2_000)]
    seg = _seg([_para(1, 0.0, head_text), *middle, _para(2, 0.0, tail_text)])

    out = format_segmentation(seg)

    assert out.startswith("file: doc.pdf")
    assert head_text in out
    assert tail_text in out
    assert "[...middle truncated...]" in out
