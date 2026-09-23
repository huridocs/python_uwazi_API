"""Seam for fetching an external URL's body (source-page HTML extraction).

The admin agent's read boundary currently reaches only Uwazi's own API — raw
entities, file bytes, segmentation. That means an entity whose extraction values
live on a *referenced* page (its ``Source Page URL`` — a ``link`` metadata
property — or a URL attachment) is unreadable: ``extract_file_refs`` skips URL
attachments (no stored bytes), so ``peek_file_text`` / ``get_file_bytes`` cannot
see them. This port extends the boundary to an operator-supplied ``http(s)`` URL,
so extraction runs even when an entity has no uploaded supporting file.

Async by signature (matching the other repository ports); the underlying
``requests`` call is synchronous (matching :class:`UwaziFileRepository`).
Implementations MUST enforce an ``http``/``https`` allow-list and a byte cap
(see :class:`HttpUrlFetcherAdapter`): a URL fetched here is operator-supplied and
unbounded content/redirects would otherwise leak into the LLM context.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class UrlFetchError(Exception):
    """Raised when an external URL cannot be fetched or decoded.

    A fetch failure must never crash a bulk extraction run: the exec helper
    (``get_url_text``) catches it and returns ``None``, and the authoring tool
    (``peek_url_text``) catches it and returns an error string.
    """


class UrlFetcherPort(ABC):
    """Fetch an external URL's body as decoded text."""

    @abstractmethod
    async def fetch_text(self, url: str) -> str:
        """Fetch ``url`` and return its body decoded as UTF-8 text.

        Raises :class:`UrlFetchError` on any failure — a disallowed scheme, a
        network error, a non-2xx status, an oversized body, or undecodable bytes.
        """
        ...
