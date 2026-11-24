"""Task export: the runs table as CSV, prompts included (pure).

The operator needs the prompts outside the app — to review them in a
spreadsheet, to diff two generations, or to hand a task's wording to someone
else. The table itself shows only the run name and its counters, so this module
turns the same rows into a CSV with the full prompt and the validated prompt
alongside the counts.

Pure: no I/O, no NiceGUI. The runner service reads the manifests and passes the
rows in; the web driver only offers the bytes to the browser.
"""

from __future__ import annotations

import csv
import io
from typing import Any, Iterable

# Header order is the operator's reading order: identity, state, counts, then
# the prompts (which are long and belong last), then the error.
EXPORT_COLUMNS: tuple[str, ...] = (
    "run_id",
    "status",
    "created_at",
    "last_executed_at",
    "modified",
    "deleted",
    "created",
    "rewired",
    "prompt",
    "validated_prompt",
    "error",
)


def _cell(value: Any) -> str:
    """One CSV cell as text (``None`` -> empty, datetimes -> ISO)."""
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    return str(value)


def tasks_csv(rows: Iterable[dict[str, Any]]) -> str:
    """Render task rows as CSV text, prompts included (pure).

    ``rows`` entries use the export column names. A missing key becomes an
    empty cell, so a run without a ``validated_prompt`` (never validated) does
    not shift the columns. ``QUOTE_ALL`` keeps a multi-line prompt inside one
    cell for every spreadsheet importer.
    """
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(EXPORT_COLUMNS), extrasaction="ignore", quoting=csv.QUOTE_ALL)
    writer.writeheader()
    for row in rows:
        writer.writerow({column: _cell(row.get(column)) for column in EXPORT_COLUMNS})
    return buffer.getvalue()


__all__ = ["EXPORT_COLUMNS", "tasks_csv"]
