"""Pure batch splitting for the parallel script helpers.

The parallel write helpers chunk a whole-list payload into per-task batches
(one port call each) so the executor can run them concurrently while each
task stays a familiar "list in, results out" port call — the same shape the
sequential helpers already make, just N of them at once. Pure: no I/O.
"""

from __future__ import annotations

from typing import Any


def split_batches(items: list[Any], size: int) -> list[list[Any]]:
    """Split ``items`` into consecutive chunks of at most ``size`` (order preserved).

    Slices are new lists sharing the item references — cheap at bulk scale
    (10 000 items become 200 chunk lists of references, no data copied).
    ``size`` must be positive; anything else fails loudly rather than
    silently producing one oversized batch.
    """
    if size <= 0:
        raise ValueError(f"batch size must be positive, got {size}")
    return [items[i : i + size] for i in range(0, len(items), size)]
