"""Isolated unit tests for window-safe search paging (per AGENTS.md).

Pure: a literal ``(start_from, batch_size, window)`` in, page plans out. No
instance, network, or stand-ins.
"""

from uwazi_api.domain.search_pages import MAX_RESULT_WINDOW, search_page_sizes


def test_a_small_request_is_one_page() -> None:
    assert list(search_page_sizes(0, 30)) == [(0, 30)]


def test_requests_never_leave_the_search_window() -> None:
    """``from + size`` above ``index.max_result_window`` is rejected by Elasticsearch."""
    for start, size in [(0, 25_000), (9_990, 30), (0, 10_000), (5_000, 9_000)]:
        for page_from, page_size in search_page_sizes(start, size):
            assert page_from + page_size <= MAX_RESULT_WINDOW


def test_a_request_wider_than_the_window_yields_the_reachable_prefix() -> None:
    assert list(search_page_sizes(0, 25_000)) == [(0, MAX_RESULT_WINDOW)]


def test_a_page_straddling_the_window_is_shortened() -> None:
    assert list(search_page_sizes(9_990, 30)) == [(9_990, 10)]


def test_starting_at_the_window_yields_nothing() -> None:
    """No request can reach offset 10 000, so the plan is empty rather than invalid."""
    assert list(search_page_sizes(MAX_RESULT_WINDOW, 30)) == []


def test_a_request_with_a_smaller_window_pages_consecutively() -> None:
    assert list(search_page_sizes(0, 250, window=100)) == [(0, 100)]


def test_zero_or_negative_sizes_yield_nothing() -> None:
    assert list(search_page_sizes(0, 0)) == []
    assert list(search_page_sizes(0, -5)) == []
