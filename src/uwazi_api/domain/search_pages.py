"""Window-safe page planning for Uwazi's Elasticsearch-backed search.

Elasticsearch rejects a request whose ``from + size`` exceeds
``index.max_result_window`` (default 10 000). Uwazi does not raise that limit
(``database/elastic_mapping/settings.js`` sets only analysis, replica, and
field-count settings), so a search can never reach past the first 10 000 hits.
Requesting ``from=10000, size=10000`` therefore fails outright, which is why the
page plan below stops at the window instead of walking off it.
"""

from __future__ import annotations

from typing import Iterator

MAX_RESULT_WINDOW = 10_000  # Elasticsearch index.max_result_window default


def search_page_sizes(
    start_from: int = 0,
    batch_size: int = 30,
    window: int = MAX_RESULT_WINDOW,
) -> Iterator[tuple[int, int]]:
    """Yield ``(from, size)`` requests reaching at most the search window.

    Every yielded pair satisfies ``from + size <= window``. ``batch_size`` is
    clamped to what the window still allows, so a caller asking for more rows
    than the window can hold gets the reachable prefix; comparing the number of
    rows returned with ``MAX_RESULT_WINDOW`` is the signal that rows remain
    unread.
    """
    offset = max(start_from, 0)
    remaining = max(batch_size, 0)
    while remaining > 0 and offset < window:
        size = min(remaining, window - offset)
        yield offset, size
        offset += size
        remaining -= size
