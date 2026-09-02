"""Pure pieces of the persistent cross-task file/entity cache.

The cache exists because a generated extraction script does an N+1 read per
entity (one raw-entity GET + one GET per supporting file) and the same reads
repeat across a turn's up-to-4 dry-run passes, the execute pass, and every
later task. Two very different freshness regimes apply:

- **File bytes are immutable per storage filename.** Uwazi mints
  ``${Date.now()}${random}.${ext}`` fresh on every upload
  (``app/api/files/filesystem.ts::generateFileName``), never rewrites a stored
  filename, and tears the bytes down with the owning entity — so a cached
  entry can never hold WRONG bytes, only bytes the server would now 404.
- **Entity raws are mutable** (the agent writes them at execute/revert time and
  humans edit Uwazi directly), so raw entries carry a capture time and expire
  via ``ENTITY_CACHE_TTL_SECONDS``, plus explicit write-path invalidation
  (the cached repository decorators + :class:`BackupIntercept`).

This module is the unit-test target for the pure decisions: key naming,
instance namespacing, TTL freshness, the stats value object, and its one-line
rendering. No I/O here — the on-disk store lives in
:mod:`uwazi_admin_agent.adapters.file_cache_store`.
"""

from __future__ import annotations

import hashlib
import re

from pydantic import BaseModel

# Defensively sanitize keys the way ``backup_store_adapter`` does (Uwazi
# filenames/sharedIds are expected to be safe, but a stray char must not break
# the store or escape the cache dir). The 8-char digest suffix keeps two keys
# that only differ in UNSAFE characters from colliding after sanitizing.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def safe_cache_name(key: str) -> str:
    """Filesystem-safe, collision-free cache file name for an arbitrary key.

    Deterministic: the same key always maps to the same name; two distinct
    keys never map to the same name (the digest is over the raw key).
    """
    stem = _UNSAFE.sub("_", key) or "_"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]
    return f"{stem}.{digest}"


def instance_dir_name(url: str) -> str:
    """Cache-root directory name for one Uwazi instance URL.

    The same ``sharedId`` can exist on two instances with different data, so
    caches must never bleed across instances. A hex digest keeps the dir
    filesystem-safe regardless of the URL's characters.
    """
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def is_fresh(cached_at: float, now: float, ttl_seconds: float) -> bool:
    """TTL freshness decision for a cached raw (``ttl <= 0`` disables raw caching).

    The boundary is inclusive: an entry captured exactly ``ttl_seconds`` ago is
    still fresh.
    """
    return ttl_seconds > 0 and (now - cached_at) <= ttl_seconds


class CachedRaw(BaseModel):
    """One cached entity raw plus its capture time (the TTL freshness input)."""

    cached_at: float
    raw: dict[str, object]


class FileCacheStats(BaseModel):
    """Aggregate cache counters for one run boundary — never per-file logging.

    ``*_fetches``/``*_fetch_seconds`` count real GETs served by the inner port
    (misses); ``*_hits`` were served from disk. Reset at a boundary's start and
    snapshotted at its end so each dry run / execute reports only its own
    window.
    """

    file_hits: int = 0
    file_fetches: int = 0
    file_fetch_seconds: float = 0.0
    raw_hits: int = 0
    raw_fetches: int = 0
    raw_fetch_seconds: float = 0.0
    invalidations: int = 0
    evictions: int = 0


def format_cache_stats(stats: FileCacheStats | None) -> str:
    """Render the one-line aggregate summary for logs and reports.

    ``None`` (cache disabled / not wired) renders as ``disabled`` so boundary
    lines stay uniform. Evictions and invalidations only appear when nonzero —
    they are exceptional, not routine traffic.
    """
    if stats is None:
        return "disabled"
    parts = [
        f"files: {stats.file_fetches} fetched, {stats.file_hits} hits ({stats.file_fetch_seconds:.1f}s)",
        f"raws: {stats.raw_fetches} fetched, {stats.raw_hits} hits ({stats.raw_fetch_seconds:.1f}s)",
    ]
    if stats.invalidations:
        parts.append(f"invalidations: {stats.invalidations}")
    if stats.evictions:
        parts.append(f"evictions: {stats.evictions}")
    return "; ".join(parts)
