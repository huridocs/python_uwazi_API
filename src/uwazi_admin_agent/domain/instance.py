"""The Uwazi instance registry: parsing, keying and naming instances.

Pure domain logic — no I/O, no environment reads. The admin agent can be
pointed at ONE Uwazi instance or at a COMMA-SEPARATED LIST of them; the list
is understood by ``uwazi_api.instance_urls`` and nowhere else, so every other
module works with a single instance URL.

The instance KEY is the stable namespace of an instance (the same shape and
algorithm as ``uwazi_property_filler.configuration.instance_key``); the SLUG
is its human-readable on-disk directory name.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urlsplit

# The ONE parser for a configured ``UWAZI_URL`` (one URL or a comma-separated
# list); shared by every package so the format cannot drift between them.
# Re-exported: the admin agent's callers have always imported it from here.
from uwazi_api.instance_urls import parse_instance_urls as parse_instance_urls

# Characters allowed in a slug: everything outside is replaced by ``_``.
_UNSAFE_SLUG_CHARS = re.compile(r"[^A-Za-z0-9._-]")

# The slug's readable part is truncated to this many characters; the digest
# suffix keeps the name unique.
_SLUG_STEM_LIMIT = 48


def _normalized(url: str) -> str:
    """The URL with its trailing slashes stripped (the instance's identity)."""
    return url.rstrip("/")


def instance_key(url: str) -> str:
    """A stable, filesystem-safe namespace key for one instance URL.

    Trailing slashes are stripped first so ``https://x.io`` and
    ``https://x.io/`` map to the same key. The result is
    ``sha1(normalized_url).hexdigest()[:16]`` — the same shape and algorithm
    as ``uwazi_property_filler.configuration.instance_key``, so the two
    packages agree on an instance's key.
    """
    return hashlib.sha1(_normalized(url).encode("utf-8")).hexdigest()[:16]


def instance_slug(url: str) -> str:
    """A human-readable, filesystem-safe directory name for one instance.

    The URL's host (and port, when non-default) lower-cased with every
    character outside ``[A-Za-z0-9._-]`` replaced by ``_``, truncated to 48
    characters, with an 8-character ``sha1`` suffix of the normalized URL
    appended (so two URLs that normalize to the same slug never collide).
    Returns ``_`` + the digest for a URL with no usable host.
    """
    normalized = _normalized(url)
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:8]
    host = urlsplit(normalized).netloc
    if not host:
        return f"_{digest}"
    stem = _UNSAFE_SLUG_CHARS.sub("_", host.lower())[:_SLUG_STEM_LIMIT]
    return f"{stem}-{digest}"
