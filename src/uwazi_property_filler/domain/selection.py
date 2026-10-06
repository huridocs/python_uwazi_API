"""Pure text-selection geometry for the PDF labeler.

The viewer reports each clicked word as a :class:`TextItem` (in PDF user-space,
bottom-left origin — the same space Uwazi's ``SelectionRectangle`` uses). This
module turns a clicked range of words into one rectangle per spanned line,
assuming a single-column layout: words in reading order are grouped into lines
by their vertical position, and each line's rectangle spans its selected words.
"""

from __future__ import annotations

from pydantic import BaseModel

from uwazi_api.domain.selection_rectangle import SelectionRectangle


class TextItem(BaseModel):
    """One word (or glyph run) with its PDF user-space bounding box.

    ``top`` is the *top edge* y-coordinate in bottom-left origin space (a
    higher value is higher on the page); ``height`` extends downward from it.
    """

    page: int
    text: str
    left: float
    top: float
    width: float
    height: float


# Fraction of a word's height within which two words count as the same line.
_LINE_TOLERANCE = 0.5


def word_range_to_rectangles(items: list[TextItem], start: int, end: int) -> list[SelectionRectangle]:
    """Rectangles covering ``items[start..end]``, one per spanned line.

    ``start`` and ``end`` are inclusive indices into ``items`` (reading order).
    Order does not matter: a backward selection is normalized. Raises
    :class:`ValueError` on out-of-range indices.
    """
    lo, hi = sorted((start, end))
    if lo < 0 or hi >= len(items) or lo > hi:
        raise ValueError(f"selection indices {start}..{end} out of range for {len(items)} items")
    selected = items[lo : hi + 1]

    rectangles: list[SelectionRectangle] = []
    line: list[TextItem] = []
    for item in selected:
        if line and abs(item.top - line[0].top) > _line_tolerance(line[0], item):
            rectangles.append(_line_rectangle(line))
            line = []
        line.append(item)
    if line:
        rectangles.append(_line_rectangle(line))
    return rectangles


def selection_text(items: list[TextItem], start: int, end: int) -> str:
    """The concatenated text of ``items[start..end]`` (single spaces)."""
    lo, hi = sorted((start, end))
    if lo < 0 or hi >= len(items) or lo > hi:
        raise ValueError(f"selection indices {start}..{end} out of range for {len(items)} items")
    return " ".join(item.text for item in items[lo : hi + 1] if item.text)


def _line_tolerance(a: TextItem, b: TextItem) -> float:
    return _LINE_TOLERANCE * max(a.height, b.height, 1e-6)


def _line_rectangle(line: list[TextItem]) -> SelectionRectangle:
    """The tight rectangle enclosing one line of selected words."""
    left = min(item.left for item in line)
    top = max(item.top for item in line)
    bottom = min(item.top - item.height for item in line)
    right = max(item.left + item.width for item in line)
    return SelectionRectangle(
        top=top,
        left=left,
        width=right - left,
        height=top - bottom,
        page=str(line[0].page),
    )
