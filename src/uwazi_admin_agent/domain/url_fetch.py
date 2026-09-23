"""Pure URL-fetch decisions (no I/O; unit-testable offline).

The scheme allow-list is the SSRF boundary: only ``http``/``https`` URLs the
operator already stored on an entity are ever fetched — never ``file://``,
``ftp://``, ``gopher://``, or internal schemes. ``truncate_url_text`` is the
authoring-time head+tail truncation shared with the exec path's byte cap (the
LLM context is small; a fetched page can be large).
"""

from __future__ import annotations

from urllib.parse import urlparse

_ALLOWED_SCHEMES: tuple[str, ...] = ("http", "https")

# Tail window kept when truncating: pagination/footer tables live near the end.
_DEFAULT_TAIL_CHARS: int = 50_000


def is_allowed_url(url: str) -> bool:
    """True when ``url`` parses and uses an http/https scheme with a host.

    The host check rejects bare ``http:`` / ``https:`` (nothing to fetch). No
    other validation — reachability, status, and size are the adapter's job.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme.lower() in _ALLOWED_SCHEMES and bool(parsed.netloc)


def truncate_url_text(text: str, max_chars: int, tail_chars: int = _DEFAULT_TAIL_CHARS) -> str:
    """Head+tail truncation to ``max_chars`` (pure; deterministic).

    Keeps the head and a tail window with a ``[...middle truncated...]`` marker
    and a trailing ``[truncated]`` flag, so pagination/footer content survives
    the cap. Returns ``text`` unchanged when it already fits.
    """
    if len(text) > max_chars:
        head = max_chars - tail_chars
        return text[:head] + "\n[...middle truncated...]\n" + text[-tail_chars:] + "[truncated]"
    return text
