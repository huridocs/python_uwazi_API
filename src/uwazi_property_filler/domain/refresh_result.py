"""Outcome of one ``Connect & Refresh``."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RefreshResult:
    """What a refresh managed to list, and what it could not reach.

    Every Uwazi search is capped at Elasticsearch's ``index.max_result_window``
    (10 000), a limit the instance does not raise and this client cannot page
    past. ``truncated_buckets`` names the searches that filled that window, so
    the UI can say the corpus is larger than the rows on screen instead of
    silently presenting a short list as complete. An empty list means every
    search finished on its own.
    """

    count: int
    merged: int = 0
    truncated_buckets: list[str | None] = field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        return not self.truncated_buckets

    def summary(self) -> str:
        """One-line message for the operator."""
        message = f"Refreshed: {self.count} documents"
        if self.is_complete:
            return message
        named = ", ".join("all documents" if value is None else f'"{value}"' for value in self.truncated_buckets)
        return (
            f"{message} — reached the 10 000-hit search limit for {named}; "
            "more documents exist on the instance. Narrow the filter to list them."
        )
