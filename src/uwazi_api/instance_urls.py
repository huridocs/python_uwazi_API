"""Parsing for a configured ``UWAZI_URL``: one instance or a comma-separated list.

``UWAZI_URL`` may name ONE Uwazi instance or SEVERAL:

    UWAZI_URL=http://localhost:3000
    UWAZI_URL=http://localhost:3000,https://uwazi.example.org

This module is the one place that understands the list. Every consumer either
works with the whole list (the admin web's instance picker, which binds the
operator's choice to the session) or targets exactly one instance and resolves
it with :func:`first_instance_url` — the same first-configured-URL convention
the CLI and the property filler use.

Pure: no I/O, no environment reads. Callers pass the raw configured value.
"""

from __future__ import annotations


def parse_instance_urls(raw: str | None) -> list[str]:
    """The configured instance URLs, in order, de-duplicated.

    Splits on commas, strips whitespace, drops empties, and de-duplicates
    while preserving the first occurrence's order. An empty/None raw yields
    an empty list.
    """
    if not raw:
        return []
    seen: set[str] = set()
    urls: list[str] = []
    for part in raw.split(","):
        url = part.strip()
        if not url or url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def first_instance_url(raw: str | None) -> str | None:
    """The single instance a one-instance consumer targets, or None when unset.

    The FIRST configured URL wins, so a shared ``UWAZI_URL`` list works for a
    consumer that administers one instance without any per-consumer setting.
    ``None``/empty/blank input yields ``None`` so the caller can raise its own
    "not configured" error.
    """
    urls = parse_instance_urls(raw)
    return urls[0] if urls else None


__all__ = ["first_instance_url", "parse_instance_urls"]
