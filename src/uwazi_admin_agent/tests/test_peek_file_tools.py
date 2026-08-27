"""Isolated unit tests for the peek_file_text truncation helper (per AGENTS.md).

Pure tests only: literal string inputs, real ``_truncate_peek``, no mocks, no
network, no repository ports, no running Uwazi instance.
"""

from uwazi_admin_agent.configuration import MAX_PEEK_CHARS
from uwazi_admin_agent.use_cases.peek_file_tools import _PEEK_TAIL_CHARS, _truncate_peek


def test_truncate_peek_keeps_head_and_tail_of_large_text() -> None:
    """A 300 KB input is cut to <= MAX_PEEK_CHARS + marker overhead while both
    the first 100 chars (head) and the last 100 chars (tail) survive — footer /
    pagination tables near the file end stay visible to the extractor."""
    head = "H" * 100
    tail = "T" * 100
    middle = "x" * (300_000 - len(head) - len(tail))
    text = head + middle + tail

    out = _truncate_peek(text)

    assert out.startswith(head)
    assert out.rstrip("[truncated]").endswith(tail)
    assert "[...middle truncated...]" in out
    assert len(out) <= MAX_PEEK_CHARS + 50


def test_truncate_peek_passes_short_text_through() -> None:
    text = "short html"
    assert _truncate_peek(text) == text


def test_truncate_peek_window_matches_configuration() -> None:
    """The tail window must be smaller than the cap so the middle is non-empty."""
    assert 0 < _PEEK_TAIL_CHARS < MAX_PEEK_CHARS
