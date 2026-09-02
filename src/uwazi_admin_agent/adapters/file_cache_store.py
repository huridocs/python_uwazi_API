"""Persistent on-disk cache for file bytes + entity raws, shared across ALL tasks/runs.

Layout under ``root`` (``data/file_cache/<instance-hash>/`` — the instance
namespacing is applied by :func:`uwazi_admin_agent.drivers.runtime.build_file_cache`)::

    files/<safe_cache_name(filename)>                              # bytes as stored
    entities/<safe_cache_name(shared_id)>/<safe_cache_name(language)>.json
                                                                   # CachedRaw JSON

Two freshness regimes (see :mod:`uwazi_admin_agent.domain.file_cache`):

- File bytes are immutable per storage filename (Uwazi mints a fresh name per
  upload and only ever deletes files), so byte entries never expire and are
  never invalidated — a hit is always the true content.
- Entity raws are mutable, so each entry wraps the raw with its capture time
  and expires via ``ttl_seconds`` (``<= 0`` disables raw caching entirely).
  Our OWN writes invalidate immediately through
  :class:`~uwazi_admin_agent.ports.cache_invalidation_port.CacheInvalidationPort`
  (the raw-repository decorator on save/delete; :class:`BackupIntercept` for
  sandbox CRUD writes); direct human edits are bounded by the TTL alone.

Concurrency: every write is atomic (temp file + ``os.replace``), every read
treats any OSError as a miss, and there are no cross-process locks —
concurrent writers of one key write identical (immutable) bytes and last
rename wins, eviction races double-delete at worst.

Eviction is an amortized full scan every ``evict_scan_interval`` puts:
expired raws are dropped first (mtime as the capture-time proxy — the exact
check happens on read via ``cached_at``), then oldest-mtime entries are
deleted until the total is back under 90% of ``max_bytes``.
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, override

from loguru import logger
from pydantic import ValidationError

from uwazi_admin_agent.domain.file_cache import (
    CachedRaw,
    FileCacheStats,
    is_fresh,
    safe_cache_name,
)
from uwazi_admin_agent.ports.cache_invalidation_port import CacheInvalidationPort
from uwazi_admin_agent.ports.cache_stats_port import CacheStatsPort

# Cache key for language=None (the server-default locale row). ISO 639-1
# language codes are 2 letters, so a real language can never collide with it.
_DEFAULT_LANGUAGE_KEY: str = "default"

# After an over-cap scan, evict down to this fraction of the cap so eviction
# does not run again on the very next scan.
_EVICT_HEADROOM: float = 0.9

# Default puts between amortized eviction scans.
_DEFAULT_EVICT_SCAN_INTERVAL: int = 256


class FileCacheStore(CacheStatsPort, CacheInvalidationPort):
    """On-disk bytes+raw cache with TTL, amortized LRU eviction, aggregate stats."""

    def __init__(
        self,
        root: Path,
        max_bytes: int,
        ttl_seconds: float,
        evict_scan_interval: int = _DEFAULT_EVICT_SCAN_INTERVAL,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._root: Path = Path(root)
        self._files_dir: Path = self._root / "files"
        self._entities_dir: Path = self._root / "entities"
        self._max_bytes: int = max_bytes
        self._ttl_seconds: float = ttl_seconds
        self._evict_scan_interval: int = max(1, evict_scan_interval)
        self._clock: Callable[[], float] = clock
        self._puts_since_scan: int = 0
        self._lock: threading.Lock = threading.Lock()  # guards counters + scan counter
        # Typed Any so the **unpack into FileCacheStats(**...) satisfies the
        # synthesized __init__ for both the int and the float fields.
        self._counters: dict[str, Any] = {}

    # --- file bytes (immutable) -----------------------------------------------

    def get_file_bytes(self, filename: str) -> bytes | None:
        """Cached bytes for ``filename``, or None on miss. Empty bytes are a hit."""
        try:
            data = (self._files_dir / safe_cache_name(filename)).read_bytes()
        except OSError:
            return None
        self._bump(file_hits=1)
        return data

    def put_file_bytes(self, filename: str, data: bytes) -> None:
        """Atomically store ``data`` under ``filename``; amortized eviction check."""
        self._write_atomic(self._files_dir / safe_cache_name(filename), data)
        self._maybe_evict()

    # --- entity raws (TTL) ------------------------------------------------------

    def get_raw(self, shared_id: str, language: str | None) -> dict[str, Any] | None:
        """Fresh cached raw for ``(shared_id, language)``, or None on miss/expiry.

        Corrupt/partial entries (a writer crashed mid-rename is impossible
        thanks to atomic writes, but a user could have edited the JSON) are
        treated as plain misses, never raised into the calling script.
        """
        try:
            entry = CachedRaw.model_validate_json(self._raw_path(shared_id, language).read_text(encoding="utf-8"))
        except (OSError, ValidationError):
            return None
        if not is_fresh(entry.cached_at, self._clock(), self._ttl_seconds):
            return None
        self._bump(raw_hits=1)
        return entry.raw

    def put_raw(self, shared_id: str, language: str | None, raw: dict[str, Any]) -> None:
        """Cache ``raw`` under ``(shared_id, language)`` stamped with the clock time."""
        if self._ttl_seconds <= 0:  # raw caching disabled — file bytes still cache
            return
        entry = CachedRaw(cached_at=self._clock(), raw=raw)
        self._write_atomic(self._raw_path(shared_id, language), entry.model_dump_json().encode("utf-8"))
        self._maybe_evict()

    # --- invalidation (write-path hook) -----------------------------------------

    @override
    def invalidate_entities(self, shared_ids: Sequence[str]) -> None:
        """Drop every cached language row of each shared_id (best-effort, race-tolerant)."""
        invalidated = sum(1 for sid in shared_ids if self._rm_dir(self._entities_dir / safe_cache_name(sid)))
        if invalidated:
            self._bump(invalidations=invalidated)

    # --- stats (run-boundary observability) --------------------------------------

    @override
    def reset_stats(self) -> None:
        """Zero all counters (called at the start of a run boundary)."""
        with self._lock:
            self._counters.clear()

    @override
    def snapshot_stats(self) -> FileCacheStats:
        """Read the current counters without clearing them."""
        with self._lock:
            return FileCacheStats(**self._counters)

    def note_file_fetch(self, seconds: float) -> None:
        """Account one real file GET (a miss) — called by the read-through decorator."""
        self._bump(file_fetches=1, file_fetch_seconds=seconds)

    def note_raw_fetch(self, seconds: float) -> None:
        """Account one real raw-entity GET (a miss) — called by the read-through decorator."""
        self._bump(raw_fetches=1, raw_fetch_seconds=seconds)

    # --- internals -----------------------------------------------------------------

    def _bump(self, **deltas: float) -> None:
        """Add deltas to the aggregate counters (thread-safe; worker threads bump too)."""
        with self._lock:
            for field, delta in deltas.items():
                self._counters[field] = self._counters.get(field, 0.0) + delta

    def _raw_path(self, shared_id: str, language: str | None) -> Path:
        lang_key = language if language else _DEFAULT_LANGUAGE_KEY
        return self._entities_dir / safe_cache_name(shared_id) / f"{safe_cache_name(lang_key)}.json"

    @staticmethod
    def _write_atomic(path: Path, data: bytes) -> None:
        """Temp file + atomic rename so concurrent readers never see partials.

        A crashed writer can leave a stray ``.tmp`` behind; eviction scans
        count and eventually reap those like any other entry.
        """
        tmp: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
            tmp.write_bytes(data)
            os.replace(tmp, path)
        except OSError:
            logger.warning("file cache: write failed for {}", path.name)
            if tmp is not None:
                with contextlib.suppress(OSError):
                    tmp.unlink(missing_ok=True)

    @staticmethod
    def _unlink(path: Path) -> bool:
        """Best-effort delete; False only when the OS refused (e.g. an eviction race)."""
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)
            return True
        return False

    @staticmethod
    def _rm_dir(path: Path) -> bool:
        """Recursively delete ``path``; True when it existed. Tolerates concurrent evictions."""
        if not path.is_dir():
            return False
        try:
            for child in path.iterdir():
                child.unlink(missing_ok=True) if child.is_file() else FileCacheStore._rm_dir(child)
            path.rmdir()
        except OSError:
            return False
        return True

    def _maybe_evict(self) -> None:
        """Run one eviction scan every ``evict_scan_interval`` puts (amortized)."""
        with self._lock:
            self._puts_since_scan += 1
            due = self._puts_since_scan >= self._evict_scan_interval
            if due:
                self._puts_since_scan = 0
        if due:
            self._evict()

    def _evict(self) -> None:
        """One scan: drop expired raws, then oldest-mtime entries until under the cap.

        Raw expiry here uses mtime as the capture-time proxy (cheap; the exact
        ``cached_at`` check happens on every read), so sweeping is hygiene —
        correctness never depends on it.
        """
        entries = self._scan_all()
        expired_cutoff = self._clock() - self._ttl_seconds
        kept: list[tuple[Path, int, float]] = []
        total = 0
        evicted = 0
        for path, size, mtime in entries:
            if self._is_expired_raw(path, mtime, expired_cutoff) and self._unlink(path):
                evicted += 1
                continue
            kept.append((path, size, mtime))
            total += size
        if total > self._max_bytes:
            kept.sort(key=lambda entry: entry[2])
            target = self._max_bytes * _EVICT_HEADROOM
            for path, size, _mtime in kept:
                if total <= target:
                    break
                if self._unlink(path):
                    total -= size
                    evicted += 1
        if evicted:
            self._bump(evictions=evicted)
            logger.info("file cache: evicted {} entries (cap {} bytes, now ~{} bytes)", evicted, self._max_bytes, total)
        else:
            logger.debug("file cache: evict scan clean ({} entries, ~{} bytes)", len(kept), total)

    def _scan_all(self) -> list[tuple[Path, int, float]]:
        """Collect (path, size, mtime) for every cached file under the root."""
        entries: list[tuple[Path, int, float]] = []
        for sub in (self._files_dir, self._entities_dir):
            if not sub.is_dir():
                continue
            for path in sub.rglob("*"):
                try:
                    if not path.is_file():
                        continue
                    stat = path.stat()
                except OSError:
                    continue  # raced with another process's eviction
                entries.append((path, stat.st_size, stat.st_mtime))
        return entries

    @staticmethod
    def _is_expired_raw(path: Path, mtime: float, cutoff: float) -> bool:
        """True for a raw-entry JSON older than the cutoff (byte files never expire)."""
        return path.suffix == ".json" and mtime < cutoff
