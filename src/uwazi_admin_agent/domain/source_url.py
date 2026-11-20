"""Pure resolution of an entity's Source Page URL (no I/O).

A source page is stored as a ``link`` metadata property: ``{"label", "url"}``
for a single value or a list of them for a repeated one. The bound
``get_url_text`` helper is entity-scoped precisely so a generated script cannot
fetch an arbitrary URL: it asks for a sharedId and the host resolves the URL
from that entity's own metadata with this function.

Pure: walks a raw dict, no network, unit-testable offline.
"""

from __future__ import annotations

from typing import Any


def _first_url(value: Any) -> str | None:
    """The first ``http(s)`` URL in one metadata value (scalar or list form)."""
    candidates = value if isinstance(value, list) else [value]
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        url = candidate.get("url")
        if isinstance(url, str) and url.lower().startswith(("http://", "https://")):
            return url
    return None


def find_source_page_url(raw: dict[str, Any]) -> str | None:
    """Return the first ``http(s)`` URL carried by an entity's link metadata.

    ``None`` when the entity has no link-shaped value, which the caller treats
    exactly like an entity without a source page (missing, continue). The
    property NAME is not assumed: a ``link``-typed property can be named
    anything, so every metadata value is inspected for the read shape.
    """
    metadata = raw.get("metadata") or {}
    if not isinstance(metadata, dict):
        return None
    for value in metadata.values():
        url = _first_url(value)
        if url is not None:
            return url
    return None


__all__ = ["find_source_page_url"]
